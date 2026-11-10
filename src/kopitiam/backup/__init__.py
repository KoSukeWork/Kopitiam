"""手动触发的 WebDAV 加密备份与恢复；所有工作目录均在 KOPITIAM_HOME 内。"""
from __future__ import annotations

import json
import ntpath
import os
import re
import shutil
import socket
import sqlite3
import stat
import tempfile
import threading
import zipfile
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timedelta
from pathlib import Path

import psutil

from .. import db
from ..config import BACKUP_KEEP, data_dir, db_path, files_dir, safe
from . import crypto, store
from .webdav import TIME_RE, WebDAV, validate_name

BUSY = "正在备份 / 恢复，请等它结束"
READ_ONLY = "正在恢复 / 已恢复，请重开 kopitiam web"


def _file_lock():
    # NOTE: 同一数据目录里多个网页进程也共用这把锁。
    import msvcrt
    data_dir().mkdir(parents=True, exist_ok=True)
    handle = (data_dir() / "backup.lock").open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return handle
    except OSError:
        handle.close()
        raise ValueError(BUSY) from None


def other_processes() -> list[str]:
    """恢复不能让仍活着的 MCP 使用旧的身份 id；只检查，不关闭任何进程。"""
    ignored = {os.getpid(), os.getppid()}
    found = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            info = process.info
            if info["pid"] in ignored:
                continue
            args = info["cmdline"] or []
            named = any("kopitiam" in arg.lower() for arg in args)
            if named and any(arg.lower() in ("mcp", "wake") for arg in args):
                found.append(f"{info['name']}（PID {info['pid']}）")
            elif not args and "kopitiam" in (info["name"] or "").lower():
                found.append(f"{info['name']}（PID {info['pid']}，无法读取运行模式）")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def _check_processes() -> None:
    active = other_processes()
    if active:
        raise ValueError("先关掉这些 agent（它们的 kopitiam MCP 或 wake 还在运行）：" + "、".join(active))


def _snapshot(source: Path, destination: Path) -> None:
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")


def _write_database(source: Path) -> None:
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src, closing(db.connect()) as dst:
        src.backup(dst)
    db.init_db()


def _local_files() -> list[Path]:
    if not files_dir().exists():
        return []
    # NOTE: 附件只打包第一层普通文件；子目录、符号链接和 junction 都跳过。
    return sorted((p for p in files_dir().iterdir() if p.is_file() and not p.is_symlink() and not p.is_junction()), key=lambda p: p.name)


@contextmanager
def _workspace():
    root = data_dir() / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="backup-", dir=root) as temp:
            yield Path(temp)
    finally:
        try:
            root.rmdir()  # 只删除空的临时目录，不碰其他任务留下的文件。
        except OSError:
            pass


def _pack(temp: Path, with_files: bool, update) -> Path:
    update("快照")
    _snapshot(db_path(), temp / "chat.db")
    attachments = _local_files() if with_files else []
    size = (temp / "chat.db").stat().st_size + sum(p.stat().st_size for p in attachments)
    with closing(sqlite3.connect(temp / "chat.db")) as c:
        version = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    manifest = {"format": 1, "app": "kopitiam", "schema_version": version, "created_at": db.now_iso(),
                "host": socket.gethostname(), "with_files": with_files, "files": len(attachments), "bytes": size}
    update("打包")
    archive = temp / "backup.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        z.write(temp / "chat.db", "chat.db")
        for p in attachments:
            z.write(p, "files/" + p.name)
    return archive


def _host_name() -> str:
    return re.sub(r"[^\w.\-]", "_", safe(socket.gethostname()))


def _new_name(listing: list[dict]) -> str:
    existing = {item["name"] for item in listing}
    stamp = datetime.now()
    while True:
        name = f"kopitiam-{_host_name()}-{stamp:%Y%m%d-%H%M%S}.kpbak"
        if name not in existing:
            return name
        # NOTE: 同秒多次备份用下一个可用的秒，避免覆盖，仍保持统一的文件名格式。
        stamp += timedelta(seconds=1)


def _prune(client: WebDAV) -> None:
    own = []
    for entry in client.list():
        match = TIME_RE.fullmatch(entry["name"])
        if match and match["host"] == _host_name() and entry["time"]:
            own.append(entry)
    own.sort(key=lambda item: item["time"], reverse=True)
    for entry in own[BACKUP_KEEP:]:
        client.delete(entry["name"])


def _archive_name(name: str) -> bool:
    if name in ("manifest.json", "chat.db"):
        return True
    if not name.startswith("files/"):
        return False
    leaf = name[6:]
    return bool(leaf and ".." not in leaf and "/" not in leaf and "\\" not in leaf and not ntpath.isreserved(leaf))


def _validate_archive(archive: Path, target: Path) -> tuple[dict, list[Path]]:
    target.mkdir()
    try:
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            seen = set()
            for info in infos:
                name = info.filename
                mode = info.external_attr >> 16
                if (not _archive_name(name) or name.casefold() in seen or info.is_dir() or "\0" in info.orig_filename
                        or stat.S_ISLNK(mode) or info.flag_bits & 1):
                    raise ValueError("备份 ZIP 包含不允许的路径、重复文件或链接")
                seen.add(name.casefold())
            if not {"manifest.json", "chat.db"} <= seen:
                raise ValueError("备份缺少 manifest.json 或 chat.db")
            if z.getinfo("manifest.json").file_size > 65536 or len(infos) > 100000:
                raise ValueError("备份清单过大")
            if sum(i.file_size for i in infos) + 64 * 1024 * 1024 > shutil.disk_usage(target).free:
                raise ValueError("本机空间不足，无法安全解开备份")
            manifest = json.loads(z.read("manifest.json"))
            if (not isinstance(manifest, dict) or manifest.get("app") != "kopitiam"
                    or type(manifest.get("format")) is not int or manifest["format"] != 1):
                raise ValueError("不认识这份备份的格式")
            version = str(manifest.get("schema_version", ""))
            if not version.isdigit():
                raise ValueError("备份数据库版本不合法")
            if int(version) > int(db.SCHEMA_VERSION):
                raise ValueError("这份备份来自更新版本的 kopitiam，先升级")
            if type(manifest.get("with_files")) is not bool or type(manifest.get("files")) is not int:
                raise ValueError("备份附件清单不合法")
            files = [i for i in infos if i.filename.startswith("files/")]
            if manifest["files"] != len(files) or (files and not manifest["with_files"]):
                raise ValueError("备份附件清单与内容不符")
            for info in infos:
                dest = target / info.filename
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
        with closing(sqlite3.connect((target / "chat.db").as_uri() + "?mode=ro", uri=True)) as c:
            c.execute("PRAGMA trusted_schema=OFF")
            if c.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("备份数据库完整性检查失败")
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"agents", "messages", "attachments", "meta"} <= tables:
                raise ValueError("这不是有效的 kopitiam 数据库")
            actual = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if not actual or str(actual[0]) != version:
                raise ValueError("备份清单与数据库版本不符")
            with closing(db.connect()) as current:
                if c.execute("PRAGMA page_size").fetchone()[0] != current.execute("PRAGMA page_size").fetchone()[0]:
                    raise ValueError("备份数据库页大小与当前数据库不同，不能恢复")
        # NOTE: 即使没有附带附件，也把原机器的绝对路径重写到当前 files/，跨目录恢复才可用。
        with closing(sqlite3.connect(target / "chat.db")) as c:
            c.execute("PRAGMA trusted_schema=OFF")
            for aid, path in c.execute("SELECT id, path FROM attachments").fetchall():
                leaf = ntpath.basename(path)
                if not _archive_name("files/" + leaf):
                    raise ValueError("备份数据库包含不合法的附件路径")
                c.execute("UPDATE attachments SET path=? WHERE id=?", (str(files_dir() / leaf), aid))
            c.commit()
        return manifest, [target / info.filename for info in files]
    except (zipfile.BadZipFile, sqlite3.DatabaseError, UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise ValueError("备份文件损坏或数据库校验失败") from exc


def _saved_directory() -> Path:
    base = data_dir() / f"restore-saved-{datetime.now():%Y%m%d-%H%M%S}"
    path, index = base, 1
    while path.exists():
        path = base.with_name(base.name + f"-{index}")
        index += 1
    path.mkdir()
    return path


def _copy_atomic(src: Path, dst: Path) -> None:
    with crypto._output(dst) as out, src.open("rb") as source:
        shutil.copyfileobj(source, out, 1024 * 1024)


def _apply_restore(temp: Path, attachments: list[Path], update) -> str:
    _check_processes()  # 下载期间新开的 MCP 也必须关掉。
    update("保留本机副本")
    saved = _saved_directory()
    _snapshot(db_path(), saved / "chat.db")
    (saved / "files").mkdir()
    moved, created = [], []
    started = False
    try:
        for source in attachments:
            target = files_dir() / source.name
            if target.is_symlink() or target.is_junction() or (target.exists() and not target.is_file()):
                raise ValueError("本机同名附件不是普通文件，不能覆盖：" + source.name)
            if target.exists() and db.file_sha256(source) != db.file_sha256(target):
                old = saved / "files" / source.name
                shutil.move(target, old)
                moved.append((old, target))
        update("写回")
        started = True
        _write_database(temp / "chat.db")
        for source in attachments:
            target = files_dir() / source.name
            if not target.exists():
                created.append(target)
                _copy_atomic(source, target)
        with closing(db.connect()) as c:
            messages = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return (f"已恢复 {messages} 条消息、{len(attachments)} 个附件。原数据副本：{saved}。"
                "现在关掉 kopitiam web 再重新打开，再重开各个 agent。")
    except Exception as exc:
        try:
            if started:
                _write_database(saved / "chat.db")
            for target in created:
                target.unlink(missing_ok=True)
            for old, target in moved:
                _copy_atomic(old, target)
        except Exception:
            raise ValueError(f"恢复失败，自动回退未完成。原数据副本在 {saved}，请先保留它并重开 kopitiam web。") from exc
        raise ValueError(f"恢复失败，原数据已保留。副本：{saved}。{exc}") from exc


class BackupManager:
    def __init__(self, *, on_restore=None, write_guard=None):
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread = None
        self.on_restore = on_restore or (lambda: None)
        self.write_guard = write_guard or nullcontext
        self.read_only = False
        self._job = {"kind": None, "state": "idle", "step": "", "progress": 0, "message": "", "started_at": None, "finished_at": None}

    def status(self) -> dict:
        with self._state_lock:
            job = dict(self._job)
        size = 0
        for path in _local_files():
            try:
                size += path.stat().st_size
            except FileNotFoundError:
                pass  # 附件可能刚被清理。
        return {"config": store.public(), "job": job, "last_backup": store.last_backup(),
                "files_bytes": size, "restart_required": self.read_only}

    def _update(self, step: str, progress: int = 0, **extra):
        with self._state_lock:
            self._job.update(step=step, progress=progress, **extra)

    def _client(self, **options):
        values = store.credentials(**options)
        password = values.pop("backup_password", None)
        return WebDAV(**values), password

    def list(self):
        client, _ = self._client()
        client.ensure_dir()
        return client.list()

    def test(self):
        return len(self.list())

    def delete(self, name: str):
        validate_name(name)
        if not self._lock.acquire(blocking=False):
            raise ValueError(BUSY)
        try:
            with _file_lock():
                client, _ = self._client()
                client.delete(name)
        finally:
            self._lock.release()

    def start(self, kind: str, *, with_files=True, name=None, backup_password=None) -> dict:
        if kind not in ("backup", "restore"):
            raise ValueError("不认识这项备份操作")
        if kind == "restore":
            validate_name(name)
            _check_processes()
        if type(with_files) is not bool or (backup_password is not None and not isinstance(backup_password, str)):
            raise ValueError("备份参数不合法")
        if not self._lock.acquire(blocking=False):
            raise ValueError(BUSY)
        lockfile = None
        try:
            client, password = self._client(require_backup=True, backup_password=backup_password)
            lockfile = _file_lock()
            with self._state_lock:
                self._job = {"kind": kind, "state": "running", "step": "准备", "progress": 0, "message": "",
                             "name": name, "needs_password": False, "started_at": db.now_iso(), "finished_at": None}
            self._thread = threading.Thread(target=self._run, args=(kind, client, password, with_files, name, lockfile), daemon=True)
            self._thread.start()
            with self._state_lock:
                return dict(self._job)
        except Exception:
            if lockfile is not None:
                lockfile.close()
            self._lock.release()
            raise

    def _run(self, kind, client, password, with_files, name, lockfile):
        try:
            with _workspace() as temp:
                if kind == "backup":
                    archive = _pack(temp, with_files, self._update)
                    client.ensure_dir()
                    name = _new_name(client.list())
                    self._update("加密", name=name)
                    encrypted = temp / name
                    crypto.encrypt_file(archive, encrypted, password)
                    self._update("上传")
                    client.upload(encrypted, name, lambda n, total: self._update("上传", int(n * 100 / max(1, total))))
                    store.record_backup({"time": db.now_iso(), "name": name, "size": encrypted.stat().st_size})
                    self._update("清理")
                    _prune(client)
                    message = "备份完成：" + name
                else:
                    self._update("暂停后台服务")
                    self.read_only = True
                    self.on_restore()
                    self._update("下载")
                    encrypted, archive = temp / name, temp / "backup.zip"
                    client.download(name, encrypted, lambda n, total: self._update("下载", int(n * 100 / max(1, total)) if total else 0))
                    self._update("解密")
                    crypto.decrypt_file(encrypted, archive, password)
                    self._update("校验")
                    _, attachments = _validate_archive(archive, temp / "checked")
                    with self.write_guard():
                        message = _apply_restore(temp / "checked", attachments, self._update)
            self._update("完成", 100, state="done", message=message)
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ValueError, OSError)) else "备份操作失败，请检查文件与网络后重试"
            self._update("失败", state="failed", message=message,
                         needs_password=isinstance(exc, crypto.CryptoError) and str(exc) == crypto.BAD_PASSWORD)
        finally:
            self._update(self._job["step"], self._job["progress"], finished_at=db.now_iso())
            lockfile.close()
            self._lock.release()
