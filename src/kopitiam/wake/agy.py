"""agy（Antigravity CLI）适配器：只弹 Windows 通知提醒用户，绝不驱动 agy。

Google 不允许别的程序或 agent 把提示词交给用个人订阅登录的 agy（算「第三方 agent 包装」，会被自动封号）。
所以这里的「叫醒」只是告诉用户：有人点名发消息给 agy 了。由用户自己到 agy 的窗口里说「看一下聊天室」，
agy 再用 kopitiam 的 MCP 工具去读。不碰 agy 的进程、不写它的文件、不调用它的任何接口。

- 每一批只提醒一次；同一个 agy 身份只留最新一条通知（按 tag 替换）。
- 它读完那一批以后（last_read 到了），把通知从通知中心收掉，免得以后看到过时的提醒。
- 你自己发的消息不弹：你知道要去叫它。
"""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from .. import db
from ..config import wake_dir
from . import notify, signals

NOTIFY_ONLY = True  # 网页据此显示铃铛、写「只提醒你」，不显示成「会被自动唤醒」
HINT = ("agy 不能自动唤醒（Google 不允许别的程序驱动个人订阅的 agy）：收到点名消息时弹通知提醒你，"
        "你在它的窗口里说「看一下聊天室」，它才去读")


def _marker(agent_id: int) -> Path:
    return wake_dir() / "agy-toast" / f"{agent_id}.json"


def _tag(agent_id: int) -> str:
    return f"agy-{agent_id}"


def check(agent: dict) -> str | None:
    return None  # 能弹通知就行；感知模块只把开着的 agy 交过来


def hint(agent: dict) -> str | None:
    return HINT


def wake(call) -> str:
    if call.senders == ["user"]:
        return "等你在 agy 窗口里让它读（你自己发的，不弹通知）"
    if not signals.take(f"agy-toast-{call.wake_id}"):
        return "已经弹过通知，等你在 agy 窗口里让它读"
    with closing(db.connect()) as c:
        row = c.execute("SELECT cwd FROM agents WHERE id=?", (call.agent_id,)).fetchone()
    where = f"（工作目录 {Path(row['cwd']).name}）" if row and row["cwd"] else ""
    who = "、".join(call.senders) or "有人"
    notify.toast(f"{call.name} 有新消息",
                 f"{who} 发来 {len(call.message_ids)} 条{where}。agy 不能自动唤醒，"
                 "请到它的窗口里说「看一下聊天室」。", tag=_tag(call.agent_id))
    marker = _marker(call.agent_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"upto": max(call.message_ids)}), encoding="utf-8")
    return "已弹通知提醒你"


def remind() -> None:
    """感知模块的后台线程定时调用：agy 读完了（或者身份没了、agy 关了），把通知收掉。"""
    folder = wake_dir() / "agy-toast"
    if not folder.is_dir():
        return
    for f in folder.glob("*.json"):
        try:
            agent_id, upto = int(f.stem), int(json.loads(f.read_text(encoding="utf-8"))["upto"])
        except (OSError, ValueError, KeyError, TypeError):
            f.unlink(missing_ok=True)
            continue
        with closing(db.connect()) as c:
            row = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if row is None or row["last_read"] >= upto or not db.is_online(row):
            f.unlink(missing_ok=True)
            notify.remove(_tag(agent_id))
