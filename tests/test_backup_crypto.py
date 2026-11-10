import hashlib
import struct

import pytest

from kopitiam.backup import crypto


@pytest.mark.parametrize("size", [0, 31, crypto.CHUNK_SIZE, crypto.CHUNK_SIZE + 1, crypto.CHUNK_SIZE * 12 + 19])
def test_crypto_roundtrip(home, size):
    src, encrypted, dst = (home / n for n in ("in", "encrypted", "out"))
    with src.open("wb") as f:
        f.truncate(size)
    crypto.encrypt_file(src, encrypted, "中文密码🔑")
    crypto.decrypt_file(encrypted, dst, "中文密码🔑")
    assert dst.stat().st_size == size
    assert hashlib.file_digest(src.open("rb"), "sha256").digest() == hashlib.file_digest(dst.open("rb"), "sha256").digest()


def blocks(raw):
    result, pos = [], crypto.HEADER.size
    while pos < len(raw):
        end = pos + 4 + struct.unpack(">I", raw[pos:pos+4])[0]
        result.append(raw[pos:end])
        pos = end
    return result


@pytest.mark.parametrize("damage", ["password", "byte", "truncate", "missing_final", "append", "reorder", "false_final"])
def test_crypto_authenticates_every_block_and_end(home, damage):
    src, encrypted, dst = (home / n for n in ("in", "encrypted", "out"))
    src.write_bytes(b"a" * crypto.CHUNK_SIZE + b"b" * crypto.CHUNK_SIZE + b"end")
    crypto.encrypt_file(src, encrypted, "password")
    raw = encrypted.read_bytes()
    header, parts = raw[:crypto.HEADER.size], blocks(raw)
    password = "password"
    if damage == "password":
        password = "wrong"
    elif damage == "byte":
        raw = raw[:80] + bytes([raw[80] ^ 1]) + raw[81:]
    elif damage == "truncate":
        raw = raw[:-1]
    elif damage == "missing_final":
        raw = header + b"".join(parts[:-1])
    elif damage == "append":
        raw += b"extra"
    elif damage == "reorder":
        raw = header + parts[1] + parts[0] + parts[2]
    elif damage == "false_final":
        cipher, prefix, _ = crypto._key(header, password)
        ct = cipher.encrypt(crypto._nonce(prefix, 2), b"end", crypto._aad(header, 2, False))
        raw = header + b"".join(parts[:-1]) + struct.pack(">I", len(ct)) + ct
    encrypted.write_bytes(raw)
    dst.write_bytes(b"existing destination")
    with pytest.raises(crypto.CryptoError) as error:
        crypto.decrypt_file(encrypted, dst, password)
    if damage in ("missing_final", "false_final", "truncate"):
        assert "不完整" in str(error.value)
    assert dst.read_bytes() == b"existing destination"
    assert not list(home.glob(".kpbak-*"))


@pytest.mark.parametrize("field, value", [(0, b"WRONG!!\n"), (1, 3), (1, 2**21), (2, 17), (3, 5),
    (6, 1), (6, 16*1024*1024+1), (7, b"\1"*6), (1, 2**20)])
def test_crypto_rejects_bad_header_before_kdf(home, monkeypatch, field, value):
    fields = [crypto.MAGIC, 2**15, 8, 1, b"s"*16, b"n"*8, crypto.CHUNK_SIZE, b"\0"*6]
    fields[field] = value
    src = home / "bad"
    src.write_bytes(crypto.HEADER.pack(*fields))
    def forbidden(*args, **kwargs):
        pytest.fail("invalid parameters reached scrypt")
    monkeypatch.setattr(crypto.hashlib, "scrypt", forbidden)
    with pytest.raises(crypto.CryptoError, match="参数不合法"):
        crypto.decrypt_file(src, home / "out", "password")


def test_crypto_randomizes_each_backup(home):
    src = home / "plain"
    src.write_bytes(b"same data")
    a, b = home / "a", home / "b"
    crypto.encrypt_file(src, a, "password")
    crypto.encrypt_file(src, b, "password")
    assert a.read_bytes() != b.read_bytes()
