"""db 层测试：config 基础工具 + 身份认领。消息收发在 test_tools.py。"""
from __future__ import annotations

import os
import subprocess
import sys
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
