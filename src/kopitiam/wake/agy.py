"""Managed ACP sessions can wake at idle; terminal-only MCP cannot."""
from __future__ import annotations

import os

from .. import db
from ..acp.managed import get_runtime, presence_locked

TERMINAL = "终端里的 agy 叫不醒：在它的窗口里说「看一下聊天室」；要自动叫醒，在网页上新建 agy 会话"


def check(agent):
    if agent.get("host") != "agy-acp":
        return TERMINAL
    row = db.get_acp(agent["id"])
    if not row:
        return "托管会话不存在"
    if row["handed_off"]:
        return "已交给终端，接回来以后才叫得醒"
    runtime = get_runtime()
    if runtime is None:
        return "agy 会话要开着 kopitiam web 才叫得醒"
    if row["pid"] != os.getpid():
        return "这个会话由另一个网页进程托管"
    if presence_locked(row["session_id"]):
        return "这个对话在终端里开着，先在终端退出"
    state = runtime.state(row)
    if state["state"] == "error":
        return state["error"] or "agy 会话出错"
    return None


def wake(call):
    runtime = get_runtime()
    if runtime is None:
        raise ValueError("agy 会话要开着 kopitiam web 才叫得醒")
    runtime.wake(call.agent_id, call.text)
