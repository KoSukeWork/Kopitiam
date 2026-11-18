"""ACP web API bodies. Authentication and the write gate stay in the HTTP handler."""
from __future__ import annotations

from .. import db
from ..config import TEXT_MAX
from . import folders
from .policy import request_paths


def state(server):
    runtime = server.acp
    sessions = {}
    for row in db.list_acp():
        status = runtime.state(row) if runtime else {
            "state": "handed_off" if row["handed_off"] else "not_loaded", "error": None,
            "permissions": [], "queued": 0}
        for card in status["permissions"]:
            card["paths"] = request_paths(card) or []
        sessions[row["name"]] = {**status, "agent_id": row["id"], "session_id": row["session_id"],
                                  "policy": row["policy"], "cwd": row["session_cwd"]}
    return {"available": runtime is not None and not runtime.closed, "sessions": sessions}


def runtime(server):
    if server.acp is None or server.acp.closed:
        raise ValueError("当前网页未启用 ACP 托管，请重新启动 kopitiam web（不加 --no-wake）")
    return server.acp


def post(server, path, data, user_id):
    if path == "/api/acp/pick-folder":
        return {"path": folders.pick(data.get("initial", ""))}
    host = runtime(server)
    name = data.get("agent")
    if path == "/api/acp/sessions":
        cwd, task = data.get("cwd"), data.get("task", "")
        if not isinstance(cwd, str) or not cwd.strip():
            raise ValueError("请填写工作目录")
        if not isinstance(task, str) or len(task) > TEXT_MAX:
            raise ValueError(f"第一条任务最多 {TEXT_MAX} 个字符")
        if data.get("name") is not None and not isinstance(data["name"], str):
            raise ValueError("名字必须是文字")
        options = {}
        for key in ("model", "effort", "mode"):
            value = data.get(key)
            if value:
                if not isinstance(value, str) or len(value) > 128:
                    raise ValueError(f"{key} 格式不正确")
                options[key] = value
        record = host.create(cwd.strip(), backend=data.get("backend", "agy"), name=data.get("name") or None,
                             policy=data.get("policy", "workspace"), options=options)
        if task.strip():
            message = db.Chat().send(user_id, task, [record["name"]])
            if message.startswith("错误："):
                raise ValueError(message)
        return {"agent": record["name"], "session_id": record["session_id"]}
    if path == "/api/acp/permission":
        host.answer(name, data.get("request"), data.get("option"))
    elif path == "/api/acp/policy":
        host.set_policy(name, data.get("policy"))
    elif path == "/api/acp/cancel":
        host.cancel_agent(name)
    elif path == "/api/acp/handoff":
        return {"commands": host.handoff(name)}
    elif path == "/api/acp/takeback":
        host.takeback(name)
    elif path == "/api/acp/retry":
        host.retry(name)
    else:
        raise ValueError("未知 ACP 操作")
    return {}
