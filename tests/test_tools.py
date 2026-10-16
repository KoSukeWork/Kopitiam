"""MCP 工具背后的 Chat 业务测试（§6）：收发、广播、附件、改名。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from kopitiam import config, db


@pytest.fixture()
def chat():
    return db.Chat()


@pytest.fixture()
def agents(home):
    a = db.claim("claude", cwd=str(home), name="A", pid=os.getpid())
    b = db.claim("codex", cwd=str(home), name="B", pid=os.getpid())
    c = db.claim("pi", cwd=str(home), name="C", pid=os.getpid())
    return a, b, c


def _message_count() -> int:
    with db._conn() as c:
        return c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def test_direct_message_visibility(home, chat, agents):
    a, b, c = agents
    out = chat.send(a, "给 B 的消息", to=["B"])
    assert out.startswith("已发送 #1 → B")
    got = chat.read(b)
    assert "给 B 的消息" in got
    assert "#1" in got and "A → B" in got
    # 发给 B 的消息：C 读不到，A 自己也读不到
    assert chat.read(c) == "没有新消息。"
    assert chat.read(a) == "没有新消息。"
    # read 第二次返回没有新消息
    assert chat.read(b) == "没有新消息。"


def test_broadcast(home, chat, agents):
    a, b, c = agents
    out = chat.send(a, "大家好")
    assert out.startswith("已发送 #1 → 所有人")  # to 为空 = 广播
    for who in (b, c):
        assert "大家好" in chat.read(who)
    # 广播大家都能读到；但 read 不返回自己发的
    assert chat.read(a) == "没有新消息。"


def test_broadcast_readable_by_everyone(home, chat, agents):
    a, b, c = agents
    chat.send(a, "广播正文")
    assert "广播正文" in chat.read(b)
    assert "广播正文" in chat.read(c)
    assert "→ 所有人" in chat.history(b)


def test_bad_target_fails_whole_send(home, chat, agents):
    a, b, c = agents
    out = chat.send(a, "x", to=["fronted"])
    assert out.startswith("错误：")
    assert '找不到 "fronted"' in out
    assert "A, B, C, user" in out  # 不猜测、不模糊匹配，只报现有名字
    assert _message_count() == 0  # 数据库里没有新增消息


def test_text_too_long_rejected(home, chat, agents):
    a, b, c = agents
    out = chat.send(a, "长" * (config.TEXT_MAX + 1))
    assert out.startswith("错误：")
    assert "长内容请写成文件用 files 发送" in out
    assert _message_count() == 0


def test_attachment_copied_into_files_dir(home, chat, agents):
    a, b, c = agents
    src = Path(home) / "计划.md"
    src.write_text("# 计划", encoding="utf-8")
    out = chat.send(a, "带附件", to=["B"], files=[str(src)])
    assert "（附件 1 个）" in out
    copies = list((Path(home) / "files").glob("000001_*_计划.md"))
    assert len(copies) == 1
    # 删掉源文件，附件还在
    src.unlink()
    assert copies[0].read_text(encoding="utf-8") == "# 计划"
    # 接收方 read 能看到附件路径
    got = chat.read(b)
    assert "📎" in got and str(copies[0]) in got


def test_attachment_relative_path_resolved_against_cwd(home, chat, agents):
    a, b, c = agents
    src = Path(home) / "rel.txt"
    src.write_text("相对路径", encoding="utf-8")
    out = chat.send(a, "相对路径附件", files=["rel.txt"])
    assert "（附件 1 个）" in out
    assert (Path(home) / "files").glob("000001_0_rel.txt")


def test_attachment_source_already_in_files_dir_not_copied(home, chat, agents):
    a, b, c = agents
    # 模拟 export_context 生成的文件：直接放到 FILES_DIR 里
    already = config.files_dir() / "export_x.md"
    already.parent.mkdir(parents=True, exist_ok=True)
    already.write_text("导出内容", encoding="utf-8")
    out = chat.send(a, "导出发送", files=[str(already)])
    assert "（附件 1 个）" in out
    # FILES_DIR 里没有多出复制文件，消息里用的还是原路径
    names = [p.name for p in config.files_dir().iterdir()]
    assert names == ["export_x.md"]
    with db._conn() as c:
        row = c.execute("SELECT path FROM attachments").fetchone()
    assert row["path"] == str(already)


def test_directory_attachment_rejected(home, chat, agents):
    a, b, c = agents
    d = Path(home) / "一个文件夹"
    d.mkdir(exist_ok=True)
    out = chat.send(a, "x", files=[str(d)])
    assert out.startswith("错误：")
    assert _message_count() == 0


def test_oversized_attachment_rejected(home, chat, agents):
    a, b, c = agents
    big = Path(home) / "big.bin"
    with open(big, "wb") as f:
        f.truncate(config.FILE_MAX + 1)  # 不真的写满数据
    out = chat.send(a, "x", files=[str(big)])
    assert out.startswith("错误：")
    assert _message_count() == 0


def test_history_shows_new_name_for_old_messages(home, chat, agents):
    a, b, c = agents
    chat.send(a, "旧消息", to=["B"])
    chat.rename(a, "frontend")
    h = chat.history(b, n=10)
    assert "frontend → B" in h
    assert "旧消息" in h
    assert "A →" not in h


def test_history_with_agent_filter(home, chat, agents):
    a, b, c = agents
    chat.send(a, "to B", to=["B"])
    chat.send(c, "to A", to=["A"])
    chat.send(b, "to C", to=["C"])
    h = chat.history(a, n=10, with_agent="B")
    assert "to B" in h
    assert "to A" not in h
    assert "to C" not in h
    # history 不改变 last_read：read 里还能读到别人发来的，且不含自己发的
    got = chat.read(a)
    assert "to A" in got
    assert "to B" not in got  # 自己发的不会出现在 read 里


def test_history_limits_and_errors(home, chat, agents):
    a, b, c = agents
    out = chat.history(a, with_agent="不存在")
    assert out.startswith("错误：")
    assert "没有历史消息。" == chat.history(b)


def test_set_cwd_requires_existing_dir(home, chat, agents):
    a, b, c = agents
    out = chat.set_cwd(a, str(home / "nope"))
    assert out.startswith("错误：")
    out = chat.set_cwd(a, str(home))
    assert out.startswith("已设置工作目录")
    assert str(home) in chat.whoami(a)


def test_messages_after_initial_latest_then_catch_up(home, chat, agents):
    a, _, _ = agents
    for i in range(5):
        chat.send(a, f"m{i}")
    # 首次加载（after=0）取最新的 limit 条
    assert [m["id"] for m in chat.messages_after(0, 2)] == [4, 5]
    # 之后按升序补，不会跳过中间的消息
    assert [m["id"] for m in chat.messages_after(1, 2)] == [2, 3]
    assert [m["id"] for m in chat.messages_after(3, 2)] == [4, 5]
