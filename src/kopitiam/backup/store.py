"""WebDAV 设置；密码只以当前 Windows 用户的 DPAPI 密文落盘。"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
import threading
from ctypes import wintypes

from ..config import data_dir
from .webdav import _origin, validate_folder, validate_url

_LOCK = threading.RLock()


def _dpapi(raw: bytes, *, decrypt=False) -> bytes:
    if os.name != "nt":
        raise ValueError("云备份只支持 Windows")
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(out)):
        raise ValueError("无法读取本机保存的密码，请重新填写" if decrypt else "无法保护密码，请检查当前 Windows 账号")
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        kernel.LocalFree(ctypes.cast(out.data, ctypes.c_void_p))


def protect(password: str) -> str:
    return base64.b64encode(_dpapi(password.encode("utf-8"))).decode("ascii")


def unprotect(value: str) -> str:
    try:
        return _dpapi(base64.b64decode(value, validate=True), decrypt=True).decode("utf-8")
    except (UnicodeError, TypeError, ValueError) as exc:
        if str(exc) == "云备份只支持 Windows":
            raise
        raise ValueError("无法读取本机保存的密码，请重新填写") from None


def _read() -> dict:
    path = data_dir() / "webdav.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, OSError):
        raise ValueError("云备份设置文件损坏或无法读取，请检查 webdav.json") from None


def _write(config: dict) -> None:
    data_dir().mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=data_dir(), prefix=".webdav-", encoding="utf-8", delete=False) as f:
            name = f.name
            json.dump(config, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, data_dir() / "webdav.json")
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def public() -> dict:
    with _LOCK:
        c = _read()
    return {"url": c.get("url", ""), "user": c.get("user", ""), "folder": c.get("folder", "kopitiam"),
            "has_password": bool(c.get("password")), "has_backup_password": bool(c.get("backup_password"))}


def save(values: dict) -> dict:
    if os.name != "nt":
        raise ValueError("云备份只支持 Windows")
    with _LOCK:
        c = _read()
        url = validate_url(values.get("url", c.get("url", "")))
        if c.get("url") and c.get("password") and _origin(url) != _origin(c["url"]) and not values.get("password"):
            raise ValueError("换了服务器要重新填应用密码")
        user = values.get("user", c.get("user", ""))
        if not isinstance(user, str) or not user.strip() or ":" in user or any(x in user for x in "\r\n"):
            raise ValueError("请填写有效的 WebDAV 用户名")
        folder = validate_folder(values.get("folder", c.get("folder", "kopitiam")))
        c.update(url=url, user=user.strip(), folder=folder)
        for field in ("password", "backup_password"):
            value = values.get(field, "")
            if not isinstance(value, str):
                raise ValueError("密码要填文字")
            if value:  # 留空是保留原值，绝不把保存的密码发回网页。
                c[field] = protect(value)
        _write(c)
    return public()


def credentials(*, backup_password: str | None = None, require_backup=False) -> dict:
    if os.name != "nt":
        raise ValueError("云备份只支持 Windows")
    with _LOCK:
        c = _read()
    if not c.get("url") or not c.get("user") or not c.get("password"):
        raise ValueError("请先设置 WebDAV 地址、用户名和应用密码")
    result = {"url": validate_url(c["url"]), "user": c["user"], "folder": validate_folder(c.get("folder", "kopitiam")),
              "password": unprotect(c["password"])}
    if require_backup:
        secret = backup_password or (unprotect(c["backup_password"]) if c.get("backup_password") else "")
        if not secret:
            raise ValueError("请先设置备份密码")
        result["backup_password"] = secret
    return result


def last_backup() -> dict | None:
    with _LOCK:
        last = _read().get("last_backup")
    if not isinstance(last, dict):
        return None
    return {key: last.get(key) for key in ("time", "name", "size")}


def record_backup(value: dict) -> None:
    with _LOCK:
        c = _read()
        c["last_backup"] = {key: value.get(key) for key in ("time", "name", "size")}
        _write(c)
