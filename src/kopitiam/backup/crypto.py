"""KPTBAK1：scrypt + 分块 AES-256-GCM，头、序号和最后一块标志均参与认证。"""
from __future__ import annotations

import hashlib
import os
import struct
import tempfile
from contextlib import contextmanager
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"KPTBAK1\n"
HEADER = struct.Struct(">8sIHH16s8sI6s")
LENGTH = struct.Struct(">I")
CHUNK_SIZE = 1024 * 1024
MAXMEM = 128 * 1024 * 1024
BAD_PASSWORD = "备份密码不对，或者文件损坏"


class CryptoError(ValueError):
    """可直接显示给用户的加密 / 完整性错误。"""


@contextmanager
def _output(dst: Path):
    """全部完成后才替换目标；失败不留下未经认证的明文或破坏已有文件。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=dst.parent, prefix=".kpbak-", delete=False) as out:
            temporary = Path(out.name)
            yield out
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, dst)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _key(header: bytes, password: str):
    if len(header) != HEADER.size:
        raise CryptoError("备份文件不完整")
    magic, n, r, p, salt, prefix, chunk_size, reserved = HEADER.unpack(header)
    # NOTE: 除 n 的上限，也限制 r/p、内存和 n*r*p，避免恶意头消耗过多内存或 CPU。
    valid = (magic == MAGIC and reserved == b"\0" * 6 and 2 <= n <= 2**20 and not n & (n - 1)
             and 1 <= r <= 16 and 1 <= p <= 4 and n * r * p <= 2**22
             and 128 * n * r + 128 * r * p + 1024 * 1024 <= MAXMEM
             and 65536 <= chunk_size <= 16 * 1024 * 1024)
    if not valid:
        raise CryptoError("备份文件头或加密参数不合法")
    try:
        key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=MAXMEM)
    except (ValueError, MemoryError) as exc:
        raise CryptoError("备份加密参数超出本机可用资源") from exc
    return AESGCM(key), prefix, chunk_size


def _nonce(prefix: bytes, index: int) -> bytes:
    if index >= 2**32:
        raise CryptoError("备份文件太大，超出加密格式限制")
    return prefix + index.to_bytes(4, "big")


def _aad(header: bytes, index: int, final: bool) -> bytes:
    return header + index.to_bytes(8, "big") + bytes([final])


def encrypt_file(src: Path, dst: Path, password: str) -> None:
    header = HEADER.pack(MAGIC, 2**15, 8, 1, os.urandom(16), os.urandom(8), CHUNK_SIZE, b"\0" * 6)
    cipher, prefix, size = _key(header, password)
    with Path(src).open("rb") as source, _output(dst) as out:
        out.write(header)
        chunk = source.read(size)
        index = 0
        while True:
            following = source.read(size)
            final = not following
            encrypted = cipher.encrypt(_nonce(prefix, index), chunk, _aad(header, index, final))
            out.write(LENGTH.pack(len(encrypted)))
            out.write(encrypted)
            if final:
                return
            chunk = following
            index += 1


def decrypt_file(src: Path, dst: Path, password: str) -> None:
    with Path(src).open("rb") as source:
        header = source.read(HEADER.size)
        cipher, prefix, size = _key(header, password)
        with _output(dst) as out:
            index = 0
            while True:
                length = source.read(LENGTH.size)
                if len(length) != LENGTH.size:
                    raise CryptoError("备份文件不完整")
                count = LENGTH.unpack(length)[0]
                if not 16 <= count <= size + 16:
                    raise CryptoError("备份文件损坏")
                encrypted = source.read(count)
                if len(encrypted) != count:
                    raise CryptoError("备份文件不完整")
                nonce = _nonce(prefix, index)
                final = False
                try:
                    plaintext = cipher.decrypt(nonce, encrypted, _aad(header, index, False))
                except InvalidTag:
                    try:
                        plaintext = cipher.decrypt(nonce, encrypted, _aad(header, index, True))
                        final = True
                    except InvalidTag:
                        raise CryptoError(BAD_PASSWORD) from None
                if final:
                    if source.read(1):
                        raise CryptoError("备份文件损坏：最后一块后还有数据")
                    out.write(plaintext)
                    return
                out.write(plaintext)
                index += 1
