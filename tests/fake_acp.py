"""Subprocess fixture: deterministic ACP peer, never starts real agents."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

write_lock = threading.Lock()
sessions = {}
permissions = {}
next_id = 0


def send(message):
    with write_lock:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        sys.stdout.flush()


def reply(ident, result):
    send({"id": ident, "result": result})


def update(sid, update_type, **fields):
    send({"method": "session/update", "params": {
        "sessionId": sid, "update": {"sessionUpdate": update_type, **fields}}})


def prompt(ident, params, cancel):
    sid = params["sessionId"]
    text = params["prompt"][0]["text"]
    if text == "crash":
        os._exit(23)
    if text == "auth":
        send({"id": ident, "error": {"code": -32000, "message": "Authentication required"}})
        return
    update(sid, "agent_message_chunk", content={"type": "text", "text": "start:" + text})
    if text in {"child", "orphan_crash"}:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        update(sid, "tool_call", toolCallId="spawn", kind="execute", title="test child", rawInput={"pid": child.pid})
        if text == "orphan_crash":
            os._exit(24)
    if text in {"permission", "slow"}:
        token = "permission:" + str(ident)
        ready = threading.Event()
        permissions[token] = [ready, None]
        send({"id": token, "method": "session/request_permission", "params": {
            "sessionId": sid, "toolCall": {"toolCallId": token, "kind": "execute",
                "title": "Test operation", "rawInput": {"CommandLine": "fake-only"}},
            "options": [{"optionId": "once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "always", "name": "Always", "kind": "allow_always"},
                        {"optionId": "deny", "name": "Deny", "kind": "reject_once"}]}})
        while not ready.wait(.01) and not cancel.is_set():
            pass
        outcome = permissions.pop(token)[1]
        if outcome:
            update(sid, "tool_call_update", toolCallId=token, status="completed", rawOutput=outcome)
    if not cancel.is_set():
        update(sid, "agent_message_chunk", content={"type": "text", "text": ":end"})
    reply(ident, {"stopReason": "cancelled" if cancel.is_set() else "end_turn"})


def handle(message):
    global next_id
    method, params, ident = message.get("method"), message.get("params", {}), message.get("id")
    if not method:
        if ident in permissions:
            permissions[ident][1] = message.get("result", message.get("error"))
            permissions[ident][0].set()
        return
    if method == "initialize":
        reply(ident, {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}})
    elif method == "echo":
        def echo():
            time.sleep(params.get("delay", 0))
            reply(ident, params["value"])
        threading.Thread(target=echo, daemon=True).start()
    elif method in {"session/new", "session/resume", "session/load"}:
        next_id += 1
        sid = params.get("sessionId", "fake-" + str(next_id))
        sessions[sid] = threading.Event()
        reply(ident, {"sessionId": sid})
    elif method in {"session/set_model", "session/set_mode", "session/set_config_option"}:
        reply(ident, {})
    elif method == "session/prompt":
        cancel = sessions[params["sessionId"]] = threading.Event()
        threading.Thread(target=prompt, args=(ident, params, cancel), daemon=True).start()
    elif method in {"session/cancel", "session/close"}:
        sessions[params["sessionId"]].set()
        if ident is not None:
            reply(ident, {})
    elif method == "test/unknown_request":
        send({"id": "unknown", "method": "unknown/client_method", "params": {}})
        reply(ident, {})
    elif method == "test/bad_frame":
        with write_lock:
            sys.stdout.write("not json\n")
            sys.stdout.flush()
    else:
        send({"id": ident, "error": {"code": -32601, "message": "Method not found"}})


for line in sys.stdin:
    handle(json.loads(line))
