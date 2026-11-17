"""Lazy ACP hosting, per-session wake queues, activity and permission waits."""
from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path

from ..config import data_dir
from .activity import Activity
from .client import ACPError, Client


BACKENDS = {"agy": {"executable": "agy", "args": ["acp", "--process-per-session", "--disable-telemetry"]}}


def backend_command(backend="agy"):
    config = BACKENDS[backend]
    executable = shutil.which(config["executable"])
    if not executable and backend == "agy":
        candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "agy/bin/agy.exe"
        if candidate.is_file():
            executable = str(candidate)
    if not executable:
        raise ACPError("找不到 agy：请先安装 Antigravity CLI")
    return [executable, *config["args"]]


@dataclass
class Session:
    session_id: str
    cwd: str
    mcp_servers: list
    options: dict = field(default_factory=dict)
    state: str = "not_loaded"
    error: str | None = None
    activity: Activity = field(default_factory=Activity)
    loaded_generation: int = 0
    queue: deque = field(default_factory=deque)
    worker: threading.Thread | None = None
    closing: bool = False
    epoch: int = 0
    metadata: dict = field(default_factory=dict)


class Host:
    def __init__(self, *, backend="agy", command=None, cwd=None, env=None,
                 permission_policy=None, on_permission=None, idle_timeout=1800,
                 before_resume=None, before_prompt=None):
        self.backend, self.command, self.env = backend, command, env
        self.cwd = str(cwd or data_dir())
        self.permission_policy, self.on_permission = permission_policy, on_permission
        self.before_resume, self.before_prompt = before_resume, before_prompt
        self.idle_timeout = idle_timeout
        self.sessions: dict[str, Session] = {}
        self._permissions = {}
        self._lock = threading.RLock()
        self._process_lock = threading.RLock()
        self._client = None
        self._generation = 0
        self._opening = 0
        self._closed = threading.Event()
        self._reaper = threading.Thread(target=self._reap_loop, name="acp-idle", daemon=True)
        self._reaper.start()

    def _get_client(self):
        with self._process_lock:
            if self._closed.is_set():
                raise ACPError("ACP host closed")
            if self._client and self._client.running:
                return self._client
            if self._client:
                self._client.close()
            self._generation += 1
            generation = self._generation
            self._client = Client(
                self.command or backend_command(self.backend), cwd=self.cwd, env=self.env,
                log_file=data_dir() / "logs" / f"{self.backend}-acp-{time.time_ns()}.log",
                on_update=self._update,
                on_exit=lambda error: self._exited(generation, error),
                request_handler=self._request,
            )
            self._client.start()
            return self._client

    def register(self, session_id, cwd, mcp_servers, *, options=None):
        with self._lock:
            if session_id in self.sessions:
                raise ValueError("Session already registered")
            session = Session(session_id, str(cwd), list(mcp_servers), dict(options or {}))
            self.sessions[session_id] = session
            return session

    def new_session(self, cwd, mcp_servers, *, options=None):
        with self._lock:
            self._opening += 1
        try:
            client = self._get_client()
            result = client.request("session/new", {"cwd": str(cwd), "mcpServers": mcp_servers}, timeout=60)
            session = self.register(result["sessionId"], cwd, mcp_servers, options=options)
            with self._lock:
                session.loaded_generation = self._generation
                session.state = "idle"
                session.metadata = result
            try:
                self._apply_options(client, session)
            except Exception:
                self.close_session(session.session_id)
                raise
            return session
        finally:
            with self._lock:
                self._opening -= 1

    def _apply_options(self, client, session):
        params = {"sessionId": session.session_id}
        if session.options.get("model"):
            client.request("session/set_config_option", {
                **params, "configId": "model", "value": session.options["model"]}, timeout=30)
        if session.options.get("effort"):
            client.request("session/set_config_option", {
                **params, "configId": "thought_level", "value": session.options["effort"]}, timeout=30)
        if session.options.get("mode"):
            client.request("session/set_mode", {**params, "modeId": session.options["mode"]}, timeout=30)

    def _ensure_loaded(self, session):
        with self._lock:
            self._opening += 1
        try:
            client = self._get_client()
            if session.loaded_generation != self._generation:
                if self.before_resume:
                    self.before_resume(session)
                result = client.request("session/resume", {
                    "sessionId": session.session_id, "cwd": session.cwd,
                    "mcpServers": session.mcp_servers}, timeout=60)
                self._apply_options(client, session)
                with self._lock:
                    session.loaded_generation = self._generation
                    session.metadata = result
            return client
        finally:
            with self._lock:
                self._opening -= 1

    def reconnect(self, session_id):
        """Validate resume and options without sending a model prompt."""
        with self._lock:
            session = self.sessions[session_id]
            if session.closing or session.worker or session.queue:
                raise ValueError("会话正在工作，请先停止这一轮")
            session.closing = True
            session.state, session.error = "working", None
            session.loaded_generation = 0
        try:
            self._ensure_loaded(session)
            with self._lock:
                session.state = "idle"
        except Exception as error:
            with self._lock:
                session.state, session.error = "error", self._error_text(error)
            raise
        finally:
            with self._lock:
                session.closing = False

    def wake(self, session_id, text):
        """Return immediately; the future completes when this queued turn ends."""
        with self._lock:
            if self._closed.is_set():
                raise ACPError("ACP host closed")
            session = self.sessions[session_id]
            if session.closing or session.state == "handed_off":
                raise ACPError("Session is not managed")
            future = Future()
            session.queue.append((text, future))
            if session.worker is None:
                session.worker = threading.Thread(target=self._work, args=(session,),
                                                  name="acp-turn", daemon=True)
                session.worker.start()
            return future

    def _work(self, session):
        while True:
            with self._lock:
                if not session.queue or self._closed.is_set() or session.closing:
                    session.worker = None
                    return
                text, future = session.queue.popleft()
                if not future.set_running_or_notify_cancel():
                    continue
                epoch = session.epoch
                session.state, session.error = "working", None
                session.activity.new_turn()
            try:
                client = self._ensure_loaded(session)
                with self._lock:
                    if epoch != session.epoch or self._closed.is_set():
                        raise ACPError("Session closed")
                if self.before_prompt:
                    self.before_prompt(session)
                result = client.request("session/prompt", {
                    "sessionId": session.session_id, "prompt": [{"type": "text", "text": text}]})
                with self._lock:
                    if epoch == session.epoch:
                        session.state = "idle"
                future.set_result(result)
            except Exception as error:
                with self._lock:
                    if epoch == session.epoch and not self._closed.is_set():
                        session.state, session.error = "error", self._error_text(error)
                    future.set_exception(error)
                    self._reject_queue(session, error)
                    session.worker = None
                return

    @staticmethod
    def _error_text(error):
        if isinstance(error, ACPError) and error.code == -32000 and "auth" in str(error).lower():
            return "agy 没登录：在终端运行一次 agy 登录（或 agy acp --login）"
        return str(error)

    def _update(self, method, params):
        if method != "session/update":
            return
        with self._lock:
            session = self.sessions.get(params.get("sessionId"))
            if session:
                update = params.get("update", {})
                session.activity.append(update)
                if update.get("sessionUpdate") == "config_option_update":
                    session.metadata["configOptions"] = update.get("configOptions", [])

    def _request(self, method, params, cancelled):
        if method != "session/request_permission":
            raise ACPError("Method not found", -32601)
        with self._lock:
            session = self.sessions.get(params.get("sessionId"))
            if not session or session.closing or self._closed.is_set():
                return {"outcome": {"outcome": "cancelled"}}
        outcome = self.permission_policy(session, params) if self.permission_policy else None
        if outcome is not None:
            return {"outcome": outcome}
        token = uuid.uuid4().hex
        permission = {"session": session, "params": params, "event": threading.Event(),
                      "outcome": {"outcome": "cancelled"}}
        with self._lock:
            if self._closed.is_set() or session.closing or cancelled.is_set():
                return {"outcome": {"outcome": "cancelled"}}
            self._permissions[token] = permission
            session.state = "waiting_permission"
        try:
            if self.on_permission:
                self.on_permission(session, token, params)
            while not permission["event"].wait(.1):
                if cancelled.is_set() or self._closed.is_set():
                    break
            return {"outcome": permission["outcome"]}
        finally:
            with self._lock:
                self._permissions.pop(token, None)
                if session.state == "waiting_permission" and not any(
                        item["session"] is session for item in self._permissions.values()):
                    session.state = "working"

    def answer_permission(self, session_id, token, option):
        with self._lock:
            item = self._permissions.get(token)
            if not item or item["session"].session_id != session_id or item["event"].is_set():
                raise ValueError("Permission request is no longer pending")
            if option not in {o["optionId"] for o in item["params"].get("options", [])}:
                raise ValueError("Unknown permission option")
            item["outcome"] = {"outcome": "selected", "optionId": option}
            item["event"].set()

    def _cancel_permissions(self, session):
        for item in self._permissions.values():
            if item["session"] is session:
                item["outcome"] = {"outcome": "cancelled"}
                item["event"].set()

    def cancel(self, session_id):
        with self._lock:
            session = self.sessions[session_id]
            client = self._client
        try:
            if client and client.running:
                client.notify("session/cancel", {"sessionId": session_id})
        finally:
            with self._lock:
                self._cancel_permissions(session)

    @staticmethod
    def _reject_queue(session, error):
        while session.queue:
            _, future = session.queue.popleft()
            if not future.done():
                future.set_exception(error)

    def close_session(self, session_id):
        with self._lock:
            session = self.sessions[session_id]
            session.closing = True
            session.epoch += 1
            self._reject_queue(session, ACPError("Session closed"))
            self._cancel_permissions(session)
            client, worker = self._client, session.worker
        try:
            if client and client.running and session.loaded_generation == self._generation:
                client.notify("session/cancel", {"sessionId": session_id})
                client.request("session/close", {"sessionId": session_id}, timeout=30)
        except Exception:
            if client:
                client.close()
            raise
        finally:
            if worker and worker is not threading.current_thread():
                worker.join(timeout=5)
            with self._lock:
                session.loaded_generation = 0
                session.state = "not_loaded"
                session.closing = False

    def _exited(self, generation, error):
        with self._lock:
            for session in self.sessions.values():
                if session.loaded_generation == generation:
                    session.loaded_generation = 0
                    session.state, session.error = "error", self._error_text(error)
                    self._cancel_permissions(session)

    def snapshot(self):
        with self._lock:
            return {key: {"state": session.state, "error": session.error,
                          "queued": len(session.queue),
                          "permissions": [{"request": token, **item["params"]}
                                          for token, item in self._permissions.items()
                                          if item["session"] is session and not item["event"].is_set()]}
                    for key, session in self.sessions.items()}

    def reap_idle(self, now=None):
        with self._process_lock:
            with self._lock:
                client = self._client
                expire = (client and client.running and not self._opening and
                          not any(s.loaded_generation == self._generation for s in self.sessions.values()) and
                          (time.monotonic() if now is None else now) - client.last_used >= self.idle_timeout)
                if expire:
                    self._client = None
            if expire:
                # Never join the reader while holding the lock used by notifications.
                client.close()

    def _reap_loop(self):
        while not self._closed.wait(30):
            self.reap_idle()

    def close(self):
        self._closed.set()
        with self._lock:
            workers = []
            for session in self.sessions.values():
                session.epoch += 1
                session.loaded_generation = 0
                session.state = "not_loaded"
                self._cancel_permissions(session)
                self._reject_queue(session, ACPError("ACP host closed"))
                if session.worker:
                    workers.append(session.worker)
        with self._process_lock:
            if self._client:
                self._client.close()
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=5)
        self._reaper.join(timeout=5)
