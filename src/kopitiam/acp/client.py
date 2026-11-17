"""Threaded, line-delimited ACP JSON-RPC transport; no ACP SDK dependency."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from .process import ProcessTree


class ACPError(RuntimeError):
    def __init__(self, message, code=None, data=None):
        super().__init__(message)
        self.code, self.data = code, data


class Client:
    def __init__(self, command, *, cwd, log_file: Path, env=None,
                 on_update=None, on_exit=None, request_handler=None):
        self.command, self.cwd, self.log_file, self.env = command, cwd, log_file, env
        self.on_update, self.on_exit = on_update, on_exit
        self.request_handler = request_handler
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, Future] = {}
        self._inbound: set[threading.Event] = set()
        self._counter = 0
        self._closed = False
        self._error = None
        self.tree = None
        self._reader = None
        self._watcher = None
        self._stderr = None
        self.last_used = time.monotonic()

    @property
    def running(self):
        return self.tree is not None and self.tree.process.poll() is None and not self._closed and not self._error

    def start(self):
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._stderr = self.log_file.open("ab")
        try:
            self.tree = ProcessTree(self.command, cwd=self.cwd, stderr=self._stderr, env=self.env)
            self._reader = threading.Thread(target=self._read, name="acp-reader", daemon=True)
            self._reader.start()
            self._watcher = threading.Thread(target=self._watch, name="acp-process", daemon=True)
            self._watcher.start()
            result = self.request("initialize", {
                "protocolVersion": 1, "clientCapabilities": {},
                "clientInfo": {"name": "kopitiam", "version": "0.1.0"},
            }, timeout=30)
            if not isinstance(result, dict) or result.get("protocolVersion") != 1:
                raise ACPError("Unsupported ACP protocol version")
            return result
        except BaseException:
            self.close()
            raise

    def _watch(self):
        code = self.tree.process.wait()
        # A child may inherit stdout and keep it open after the front process dies.
        # Do not rely on EOF to release pending prompts or kill that orphan tree.
        self._fail(ACPError(f"ACP process exited (code {code})"))
        self.tree.close()

    def _send(self, message):
        line = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            if not self.running:
                raise self._error or ACPError("ACP process is not running")
            try:
                self.tree.process.stdin.write(line)
                self.tree.process.stdin.flush()
            except (OSError, ValueError) as error:
                raise ACPError(f"ACP write failed: {error}") from error
            self.last_used = time.monotonic()

    def request(self, method, params, *, timeout=None):
        future = Future()
        with self._lock:
            if self._closed or self._error:
                raise self._error or ACPError("ACP client closed")
            self._counter += 1
            ident = self._counter
            self._pending[ident] = future
        try:
            self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
            return future.result(timeout=timeout)
        finally:
            with self._lock:
                self._pending.pop(ident, None)

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _answer(self, message, cancelled):
        try:
            if self.request_handler is None:
                raise ACPError("Method not found", -32601)
            result = self.request_handler(message["method"], message.get("params", {}), cancelled)
            reply = {"result": result}
        except ACPError as error:
            reply = {"error": {"code": error.code or -32603, "message": str(error)}}
        except Exception:
            reply = {"error": {"code": -32603, "message": "Client request handler failed"}}
        try:
            self._send({"jsonrpc": "2.0", "id": message["id"], **reply})
        except ACPError:
            pass  # Process shutdown already fails all callers.
        finally:
            with self._lock:
                self._inbound.discard(cancelled)

    def _read(self):
        error = ACPError("ACP process exited")
        try:
            for line in self.tree.process.stdout:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("JSON-RPC message must be an object")
                if "method" not in message:
                    with self._lock:
                        future = self._pending.get(message.get("id"))
                        if future is not None and not future.done():
                            if "error" in message:
                                info = message["error"]
                                future.set_exception(ACPError(info.get("message", "ACP error"),
                                                              info.get("code"), info.get("data")))
                            else:
                                future.set_result(message.get("result"))
                elif "id" in message:
                    cancelled = threading.Event()
                    with self._lock:
                        self._inbound.add(cancelled)
                    threading.Thread(target=self._answer, args=(message, cancelled),
                                     name="acp-request", daemon=True).start()
                elif self.on_update:
                    self.on_update(message["method"], message.get("params", {}))
        except Exception as cause:
            error = ACPError(f"ACP stream failed: {cause}")
        finally:
            self._fail(error)
            self.tree.close()
            if self.on_exit and not self._closed:
                self.on_exit(self._error)

    def _fail(self, error):
        with self._lock:
            self._error = self._error or error
            for cancelled in self._inbound:
                cancelled.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(self._error)

    def close(self):
        with self._lock:
            self._closed = True
        self._fail(ACPError("ACP client closed"))
        if self.tree:
            self.tree.close()
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=5)
        if self._watcher and self._watcher is not threading.current_thread():
            self._watcher.join(timeout=5)
        if self.tree:
            for pipe in (self.tree.process.stdin, self.tree.process.stdout):
                pipe.close()
        if self._stderr:
            self._stderr.close()
