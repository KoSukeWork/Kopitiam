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
from .policy import artifact_directory, decide

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
        self._paused = set()
        self._sessions = {}  # agent ID -> (host, ACP session ID)
        self.errors = {}
        self.closed = False
        self.web_url = None
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
                host.permission_policy = self._permission
                host.on_permission = self._notify_permission
                self._hosts[backend] = host
            return self._hosts[backend]

    @staticmethod
    def _check_presence(session):
        if presence_locked(session.session_id):
            raise ValueError("这个对话在终端里开着，先在终端退出")

    def _permission(self, session, params):
        with self._lock:
            agent_id = next((ident for ident, (_, sid) in self._sessions.items()
                             if sid == session.session_id), None)
        row = db.get_acp(agent_id) if agent_id is not None else None
        if row is None:
            return None
        return decide(session, params, policy=row["policy"],
                      extra_dirs=json.loads(row["options"]).get("extra_dirs", []),
                      artifacts=artifact_directory(session.session_id))

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
            if self.closed or row["id"] in self._removed or row["id"] in self._paused:
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
        with self._lock:
            row = db.get_acp(agent_id)
            if not row:
                raise ValueError("托管会话不存在")
            if presence_locked(row["session_id"]):
                raise ValueError("这个对话在终端里开着，先在终端退出")
            host, sid = self._register(row)
            return host.wake(sid, text)

    def lookup(self, name):
        if not isinstance(name, str):
            raise ValueError("请选择一个托管会话")
        row = next((r for r in db.list_acp() if r["name"].casefold() == name.casefold()), None)
        if row is None:
            raise ValueError("托管会话不存在")
        if db.is_online(row) and row["pid"] != os.getpid():
            raise ValueError("这个会话由另一个网页进程托管")
        return row

    def _notify_permission(self, session, token, params):
        from ..wake import notify
        with self._lock:
            ident = next((ident for ident, (_, sid) in self._sessions.items() if sid == session.session_id), None)
        row = db.get_acp(ident) if ident is not None else None
        if row:
            notify.toast(f"{row['name']} 等你确认", str(params.get("toolCall", {}).get("title", "确认操作"))[:200],
                         launch=self.web_url, tag=f"agy-{ident}")

    def answer(self, name, token, option):
        row = self.lookup(name)
        pair = self._sessions.get(row["id"])
        if not pair or not isinstance(token, str) or not isinstance(option, str):
            raise ValueError("权限请求已结束或格式不正确")
        pair[0].answer_permission(pair[1], token, option)
        from ..wake import notify
        notify.remove(f"agy-{row['id']}")

    def cancel_agent(self, name):
        row = self.lookup(name)
        pair = self._sessions.get(row["id"])
        if pair:
            pair[0].cancel(pair[1])
        from ..wake import notify
        notify.remove(f"agy-{row['id']}")

    def set_policy(self, name, policy):
        row = self.lookup(name)
        if policy not in ("workspace", "all"):
            raise ValueError("权限模式必须是 workspace 或 all")
        with db._conn() as connection:
            connection.execute("UPDATE acp_sessions SET policy=? WHERE agent_id=?", (policy, row["id"]))

    def activity(self, name, after=0):
        row = self.lookup(name)
        pair = self._sessions.get(row["id"])
        if pair:
            with pair[0]._lock:
                session = pair[0].sessions.get(pair[1])
                if session:
                    return session.activity.snapshot(after)
        return {"seq": 0, "reset": after != 0, "events": []}

    def handoff(self, name):
        with self._lock:
            row = self.lookup(name)
            state = self.state(row)
            if state["state"] in ("working", "waiting_permission") or state["queued"] or row["id"] in self._creating:
                raise ValueError("会话正在工作，请先停止这一轮")
            if row["id"] in self._paused:
                raise ValueError("正在切换会话的托管方式，请稍后重试")
            if not row["session_id"]:
                raise ValueError("会话尚未创建成功，不能交给终端")
            self._paused.add(row["id"])
            pair = self._sessions.get(row["id"])
        try:
            if pair:
                pair[0].close_session(pair[1])
            with db._conn() as connection:
                connection.execute("UPDATE acp_sessions SET handed_off=? WHERE agent_id=?", (db.now_iso(), row["id"]))
        finally:
            with self._lock:
                self._paused.discard(row["id"])
        quote = lambda text: "'" + text.replace("'", "''") + "'"
        return ["Set-Location -LiteralPath " + quote(row["session_cwd"]),
                "agy --conversation " + quote(row["session_id"])]

    def retry(self, name):
        with self._lock:
            row = self.lookup(name)
            if not row["session_id"]:
                raise ValueError("会话未创建成功，请删除后重新创建")
            host, sid = self._register(row)
            self._paused.add(row["id"])
        try:
            self._check_presence(host.sessions[sid])
            host.reconnect(sid)
            with db._conn() as connection:
                connection.execute("UPDATE wake_state SET woken_pid=NULL WHERE agent_id=?", (row["id"],))
        finally:
            with self._lock:
                self._paused.discard(row["id"])

    def takeback(self, name):
        with self._lock:
            row = self.lookup(name)
            if row["id"] in self._paused:
                raise ValueError("正在切换会话的托管方式，请稍后重试")
            self._paused.add(row["id"])
        try:
            if presence_locked(row["session_id"]):
                raise ValueError("这个对话在终端里开着，先在终端退出")
            with db._conn() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute("UPDATE acp_sessions SET handed_off=NULL WHERE agent_id=?", (row["id"],))
                    connection.execute("UPDATE agents SET pid=?,pid_ctime=? WHERE id=?",
                                       (os.getpid(), db._pid_ctime(os.getpid()), row["id"]))
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
        finally:
            with self._lock:
                self._paused.discard(row["id"])

    def state(self, row):
        if row["handed_off"]:
            return {"state": "handed_off", "error": None, "permissions": [], "queued": 0}
        with self._lock:
            if row["id"] in self.errors:
                return {"state": "error", "error": self.errors[row["id"]], "permissions": [], "queued": 0}
            pair = self._sessions.get(row["id"])
            if pair:
                snapshot = pair[0].snapshot().get(pair[1])
                if snapshot:
                    return snapshot
        return {"state": "not_loaded", "error": None, "permissions": [], "queued": 0}

    def remove(self, agent_id):
        with self._lock:
            if agent_id in self._creating or agent_id in self._paused:
                raise ValueError("正在创建或切换这个会话，请稍后删除")
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
