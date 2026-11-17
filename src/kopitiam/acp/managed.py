"""Bridge the generic ACP host to persistent Kopitiam identities."""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path

from .. import db
from ..config import data_dir, norm
from .host import Host

_runtimes = {}
_registry_lock = threading.RLock()


def get_runtime():
    with _registry_lock:
        return _runtimes.get(norm(data_dir()))


def presence_locked(session_id: str, *, gemini_dir: Path | None = None) -> bool:
    # Never let a persisted/imported ID select an arbitrary file.
    try:
        uuid.UUID(session_id)
    except (ValueError, TypeError, AttributeError):
        return False
    path = (gemini_dir or Path.home() / ".gemini") / "antigravity-cli/presence" / f"{session_id}.lock"
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return False
    except OSError:
        return True  # Cannot establish that the conversation is available.
    with handle:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def mcp_server(agent_id: int):
    executable = Path(sys.executable).with_name("kopitiam.exe" if os.name == "nt" else "kopitiam")
    # NOTE: Prefer the entry point beside this interpreter (also uv run's venv).
    # Embedded/alternate launches can invoke the same installed package directly.
    command = [str(executable)] if executable.is_file() else [
        sys.executable, "-c", "from kopitiam.cli import main; raise SystemExit(main())"]
    return {"name": "kopitiam", "command": command[0], "args": [*command[1:], "mcp", "--kind", "agy",
            "--agent-id", str(agent_id), "--host-pid", str(os.getpid())],
            "env": [{"name": "KOPITIAM_HOME", "value": str(data_dir().resolve())}]}


def close_agent(agent_id, row):
    runtime = get_runtime()
    if runtime and (row["pid"] == os.getpid() or not db.is_online(row)):
        runtime.remove(agent_id)
    elif db.is_online(row):
        raise ValueError("托管身份还在运行，请在托管它的网页中删除")


class Runtime:
    def __init__(self, *, host_factory=None):
        self.home = norm(data_dir())
        self._lock = threading.RLock()
        self._hosts = {}
        self._creating = set()
        self._removed = set()
        self._sessions = {}  # agent ID -> (host, ACP session ID)
        self.errors = {}
        self.closed = False
        self.host_factory = host_factory or (lambda backend: Host(backend=backend))
        with _registry_lock:
            if self.home in _runtimes:
                raise ValueError("这个数据目录已在当前进程中托管 ACP 会话")
            db.init_db()
            db.adopt_acp()
            _runtimes[self.home] = self

    def _host(self, backend):
        with self._lock:
            if self.closed:
                raise ValueError("ACP 主机已关闭")
            if backend not in self._hosts:
                host = self.host_factory(backend)
                host.before_resume = host.before_prompt = self._check_presence
                self._hosts[backend] = host
            return self._hosts[backend]

    @staticmethod
    def _check_presence(session):
        if presence_locked(session.session_id):
            raise ValueError("这个对话在终端里开着，先在终端退出")

    def _register(self, row):
        if row["archived_at"] or row["trashed_at"]:
            raise ValueError("会话已归档或在回收站中")
        if row["handed_off"]:
            raise ValueError("已交给终端，接回来以后才叫得醒")
        if row["pid"] != os.getpid():
            raise ValueError("这个会话由另一个网页进程托管")
        if not row["session_id"]:
            raise ValueError(self.errors.get(row["id"], "会话尚未建好，请重新创建"))
        with self._lock:
            if self.closed or row["id"] in self._removed:
                raise ValueError("托管会话已关闭")
            if row["id"] not in self._sessions:
                host = self._host(row["backend"])
                host.register(row["session_id"], row["session_cwd"], [mcp_server(row["id"])],
                              options=json.loads(row["options"]))
                self._sessions[row["id"]] = host, row["session_id"]
            return self._sessions[row["id"]]

    def create(self, cwd, *, backend="agy", name=None, policy="workspace", options=None):
        agent_id = db.create_acp(cwd, backend=backend, name=name, policy=policy, options=options)
        with self._lock:
            self._removed.discard(agent_id)
            self._creating.add(agent_id)
        host = None
        session = None
        try:
            host = self._host(backend)
            session = host.new_session(str(Path(cwd).resolve()), [mcp_server(agent_id)], options=options)
            db.bind_acp(agent_id, session.session_id)
            with self._lock:
                self._sessions[agent_id] = host, session.session_id
            return db.get_acp(agent_id)
        except Exception as error:
            self.errors[agent_id] = Host._error_text(error)
            if session is not None:
                host.close_session(session.session_id)
            raise ValueError(self.errors[agent_id]) from error
        finally:
            with self._lock:
                self._creating.discard(agent_id)

    def wake(self, agent_id, text):
        row = db.get_acp(agent_id)
        if not row:
            raise ValueError("托管会话不存在")
        if presence_locked(row["session_id"]):
            raise ValueError("这个对话在终端里开着，先在终端退出")
        host, sid = self._register(row)
        return host.wake(sid, text)

    def state(self, row):
        if row["handed_off"]:
            return {"state": "handed_off", "error": None, "permissions": [], "queued": 0}
        with self._lock:
            if row["id"] in self.errors:
                return {"state": "error", "error": self.errors[row["id"]], "permissions": [], "queued": 0}
            pair = self._sessions.get(row["id"])
            if pair:
                return pair[0].snapshot()[pair[1]]
        return {"state": "not_loaded", "error": None, "permissions": [], "queued": 0}

    def remove(self, agent_id):
        with self._lock:
            if agent_id in self._creating:
                raise ValueError("正在创建这个会话，请稍后删除")
            self._removed.add(agent_id)
            pair = self._sessions.get(agent_id)
        if pair:
            host, sid = pair
            host.close_session(sid)
            with host._lock:
                host.sessions.pop(sid, None)
        with self._lock:
            self._sessions.pop(agent_id, None)
            self.errors.pop(agent_id, None)

    def close(self):
        with self._lock:
            self.closed = True
            hosts = list(self._hosts.values())
        for host in hosts:
            host.close()
        with _registry_lock:
            if _runtimes.get(self.home) is self:
                _runtimes.pop(self.home)
