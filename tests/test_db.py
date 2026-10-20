"""db 层测试：config 基础工具 + 身份认领。消息收发在 test_tools.py。"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kopitiam import config, db


def test_norm_ignores_case_and_slash_direction():
    assert config.norm("E:/Proj/x") == config.norm("E:\\Proj\\x")
    assert config.norm("e:\\proj\\X") == config.norm("E:\\Proj\\x")


def test_data_dir_follows_env(home, monkeypatch):
    assert config.data_dir() == Path(home)
    assert config.files_dir() == Path(home) / "files"
    assert config.db_path() == Path(home) / "chat.db"


def test_data_dir_defaults_to_home(monkeypatch):
    monkeypatch.delenv("KOPITIAM_HOME", raising=False)
    assert config.data_dir() == Path.home() / ".kopitiam"


def test_safe_replaces_bad_chars():
    assert config.safe('a<b>:"/\\|?*.txt') == "a_b________.txt"
    assert config.safe("计划\n.md") == "计划_.md"  # 控制字符也换掉


def test_safe_truncates_but_keeps_extension():
    s = config.safe("x" * 120 + ".md")
    assert len(s) == 100
    assert s.endswith(".md")
    assert s.startswith("x" * 96)


def test_safe_empty_becomes_underscore():
    assert config.safe("") == "_"


def test_valid_name():
    assert config.valid_name("前端")
    assert config.valid_name("claude@Client-2")
    assert not config.valid_name("all")
    assert not config.valid_name("所有人")
    assert not config.valid_name("")
    assert not config.valid_name("a" * 33)
    assert not config.valid_name("带 空格")


# ---- 身份认领（§5） ----


def test_identity_name_persists_after_restart(home):
    proj = str(home / "Proj")
    a = db.claim("claude", cwd=proj, pid=os.getpid())
    chat = db.Chat()
    assert chat.whoami(a).startswith("你是 claude@Proj")
    chat.rename(a, "frontend")
    db.release(a)  # 模拟退出：pid 置空
    b = db.claim("claude", cwd=proj, pid=os.getpid())
    assert b == a  # 改过的名字下次启动还在
    assert db.Chat().whoami(b).startswith("你是 frontend")


def test_two_online_agents_get_distinct_names(home):
    proj = str(home / "Proj")
    a = db.claim("claude", cwd=proj, pid=os.getpid())
    b = db.claim("claude", cwd=proj, pid=os.getppid())
    chat = db.Chat()
    assert chat.whoami(a).startswith("你是 claude@Proj")
    assert chat.whoami(b).startswith("你是 claude@Proj-2")


def test_two_online_agents_distinct_pids(home):
    """用两个确定存在的真实 pid（当前进程 + 一个子进程）验证在线判断。"""
    proj = str(home / "Proj")
    a = db.claim("claude", cwd=proj, pid=os.getpid())
    # NOTE: 用自杀式的子进程当第二个在线进程；不主动 kill——沙箱下 kill 权限不可靠
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(3)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        b = db.claim("claude", cwd=proj, pid=proc.pid)
        chat = db.Chat()
        assert db.is_online(_row(proc.pid))
        assert chat.whoami(b).startswith("你是 claude@Proj-2")
    finally:
        proc.wait(timeout=15)


def test_named_claim_reuses_offline_suffix_rows(home):
    # --name codex 被占用时用 codex-2；codex-2 退出后，下次应认领它，而不是再建 codex-3
    proj = str(home / "Proj")
    a = db.claim("codex", cwd=proj, name="codex", pid=os.getpid())
    b = db.claim("codex", cwd=proj, name="codex", pid=os.getppid())
    assert db.Chat().whoami(b).startswith("你是 codex-2")
    db.release(b)
    c = db.claim("codex", cwd=proj, name="codex", pid=os.getppid())
    assert c == b
    with db._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agents WHERE kind='codex'").fetchone()[0] == 2
    assert a != c


def _row(pid):
    from kopitiam.db import _conn

    with _conn() as c:
        return c.execute("SELECT * FROM agents WHERE pid=?", (pid,)).fetchone()


def test_claim_rejects_bad_names(home):
    chat = db.Chat()
    with pytest.raises(ValueError):
        db.claim("claude", cwd=str(home), name="all", pid=os.getpid())
    with pytest.raises(ValueError):
        db.claim("claude", cwd=str(home), name="", pid=os.getpid())
    with pytest.raises(ValueError):
        db.claim("claude", cwd=str(home), name="a" * 33, pid=os.getpid())
    with pytest.raises(ValueError):
        db.claim("claude", cwd=str(home), name="带 空格", pid=os.getpid())
    with pytest.raises(ValueError):
        db.claim("claude", cwd=str(home), name="user", pid=os.getpid())
    a = db.claim("claude", cwd=str(home), name="前端", pid=os.getpid())
    assert chat.whoami(a).startswith("你是 前端")


def test_rename_rejects_bad_names(home):
    a = db.claim("claude", cwd=str(home), pid=os.getpid())
    chat = db.Chat()
    for bad in ("all", "", "a" * 33, "带 空格", "user"):
        out = chat.rename(a, bad)
        assert out.startswith("错误："), f"{bad!r} 应该被拒绝"
    assert chat.rename(a, "前端").startswith("已改名")


def test_new_agent_does_not_receive_old_messages(home):
    chat = db.Chat()
    a = db.claim("claude", cwd=str(home), name="A", pid=os.getpid())
    chat.send(a, "这是旧消息")
    b = db.claim("codex", cwd=str(home), name="B", pid=os.getpid())
    assert chat.read(b) == "没有新消息。"
    # 但 history 能看到（历史可查，未读不推）
    assert "这是旧消息" in chat.history(b)


# ---- 同一个 agent 开多个 MCP 进程（Codex 桌面版、dsh 的探测进程）共用一个身份 ----


def test_same_host_shares_identity(home):
    proj = str(home / "Proj")
    a = db.claim("codex", cwd=proj, name="codex", pid=os.getpid())
    b = db.claim("codex", cwd=proj, name="codex", pid=os.getpid())  # 同一个 agent 又开了一个 MCP
    assert a == b
    c1 = db.claim("claude", cwd=proj, pid=os.getpid())
    c2 = db.claim("claude", cwd=proj, pid=os.getpid())
    assert c1 == c2
    with db._conn() as c:
        names = [r[0] for r in c.execute("SELECT name FROM agents WHERE kind <> 'human' ORDER BY id")]
    assert names == ["codex", "claude@Proj"]


def test_release_keeps_identity_while_sibling_mcp_runs(home):
    a = db.claim("codex", cwd=str(home), name="codex", pid=os.getpid())
    # 模拟同一个 agent 下面另一个还在跑的 kopitiam MCP 进程
    sib = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "kopitiam", "mcp"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.5)
        db.release(a, host=os.getpid())
        assert db.is_online(_row(os.getpid()))  # 兄弟进程还在，不标离线
    finally:
        sib.terminate()
        sib.wait(10)
    db.release(a, host=os.getpid())
    assert _row(os.getpid()) is None  # 最后一个也退出了，才标离线


def test_claiming_base_name_drops_spare_suffix_rows(home):
    proj = str(home / "Proj")
    a = db.claim("codex", cwd=proj, name="codex", pid=os.getpid())
    b = db.claim("codex", cwd=proj, name="codex", pid=os.getppid())  # 另一个 agent → codex-2
    lone = db.claim("pi", cwd=proj, name="myXbot-2", pid=os.getppid())
    db.release(b)
    db.release(lone)
    db.release(a)
    db.claim("codex", cwd=proj, name="codex", pid=os.getpid())
    db.claim("pi", cwd=proj, name="my_bot", pid=os.getpid())  # _ 是 LIKE 通配符，不能误删 myXbot-2
    with db._conn() as c:
        names = {r[0] for r in c.execute("SELECT name FROM agents")}
    assert "codex-2" not in names  # 离线、没消息的多余身份被清掉
    assert "myXbot-2" in names


def test_host_pid_is_a_live_ancestor():
    import psutil

    h = db.host_pid()
    assert psutil.pid_exists(h)
    assert h in {p.pid for p in psutil.Process(os.getpid()).parents()} | {os.getpid()}


# ---- Codex：一个对话一个身份 ----


def _names(c) -> dict[int, str]:
    return {r[0]: r[1] for r in c.execute("SELECT id, name FROM agents")}


def test_claim_thread_one_identity_per_conversation(home):
    proj, other = str(home / "Client"), str(home / "Other")
    empty = db.claim("codex", cwd=proj, pid=os.getpid())  # 以前按文件夹认领的旧身份：没消息
    used = db.claim("codex", cwd=other, pid=os.getpid())  # 有消息
    db.Chat().send(db.user_id(), "给旧身份", ["codex@Other"])
    db.release(empty)
    db.release(used)

    a = db.claim_thread("codex", "t-1", proj, pid=os.getpid())
    b = db.claim_thread("codex", "t-2", proj, pid=os.getpid())  # 同一个文件夹的另一个对话
    assert db.claim_thread("codex", "t-1", proj, pid=os.getpid()) == a  # 同一个对话认领回同一个身份
    o = db.claim_thread("codex", "t-3", other, pid=os.getpid())
    with db._conn() as c:
        names = _names(c)
        threads = {r[0]: r[1] for r in c.execute("SELECT id, thread_id FROM agents")}
    assert names[a] == "codex@Client" and empty not in names  # 没消息的旧身份删掉，名字给这个对话
    assert names[b] == "codex@Client-2"
    assert o == used and threads[o] == "t-3"  # 有消息的旧身份接着用（历史和未读都在）
    assert "给旧身份" in db.Chat().read(o)

    db.Chat().rename(a, "主对话")
    assert db.claim_thread("codex", "t-1", proj, pid=os.getpid()) == a  # 改过的名字跟着对话
    # 不按对话认领时（没有对话 ID 的旧版 Codex），不会拿走绑了对话的身份
    d = db.claim("codex", cwd=proj, pid=os.getpid())
    assert d not in (a, b)


def test_adopt_reconnects_recent_desktop_conversations(home):
    proj = str(home / "Client")
    desk = db.claim_thread("codex", "t-1", proj, pid=os.getpid(), host="codex-desktop")
    cli = db.claim_thread("codex", "t-2", proj, pid=os.getpid())  # 命令行窗口里的：窗口关了就叫不醒
    stale = db.claim_thread("codex", "t-3", proj, pid=os.getpid(), host="codex-desktop")
    for i in (desk, cli, stale):
        db.release(i)  # 桌面版关了
    with db._conn() as c:
        c.execute("UPDATE agents SET last_seen='2020-01-01T00:00:00' WHERE id=?", (stale,))
    assert db.adopt("codex", "codex-desktop", os.getpid()) == 1  # 桌面版重开
    with db._conn() as c:
        online = {r["id"]: db.is_online(r) for r in c.execute("SELECT * FROM agents")}
        seen = c.execute("SELECT last_seen FROM agents WHERE id=?", (stale,)).fetchone()[0]
    assert online[desk] and not online[cli] and not online[stale]  # 很久没用的不接
    assert seen == "2020-01-01T00:00:00"
