"""NextChat：只接 MCP（所有对话共用一个身份），不做自动唤醒，也导不出对话记录。"""
from __future__ import annotations

from kopitiam import db, mcp_server
from kopitiam.cli import build_parser
from kopitiam.db import Chat
from kopitiam.wake import service


def _row(agent_id: int):
    from contextlib import closing
    with closing(db.connect()) as c:
        return c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()


def test_cli_accepts_nextchat_kind():
    args = build_parser().parse_args(["mcp", "--kind", "nextchat"])
    assert (args.kind, args.name) == ("nextchat", None)


def test_nextchat_gets_fixed_name(home):
    agent = mcp_server.start("nextchat")
    row = _row(agent)
    assert (row["name"], row["kind"]) == ("nextchat", "nextchat")
    # 重开 NextChat（启动目录可能变了）：还是同一个身份
    assert mcp_server.start("nextchat") == agent


def test_nextchat_name_can_be_overridden(home):
    agent = mcp_server.start("nextchat", "nextchat-desktop")
    assert _row(agent)["name"] == "nextchat-desktop"


def test_nextchat_export_explains_why(home):
    agent = mcp_server.start("nextchat")
    text = Chat().export_context(agent)
    assert text.startswith("错误：") and "NextChat" in text and "send" in text


def test_nextchat_wake_not_supported(home):
    agent = mcp_server.start("nextchat")
    assert "看一下聊天室" in service.check(dict(_row(agent)))
    info = service.overview()["agents"]["nextchat"]
    # 不给开关，也不当成要用户处理的警告
    assert info["supported"] is False and info["warn"] is False
    assert "看一下聊天室" in info["reason"]
