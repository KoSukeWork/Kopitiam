"""db 层测试。M2 会往里补身份认领和消息收发的测试，这里先测 config 的基础工具。"""
from __future__ import annotations

from pathlib import Path

from agent_chat import config


def test_norm_ignores_case_and_slash_direction():
    assert config.norm("E:/Proj/x") == config.norm("E:\\Proj\\x")
    assert config.norm("e:\\proj\\X") == config.norm("E:\\Proj\\x")


def test_data_dir_follows_env(home, monkeypatch):
    assert config.data_dir() == Path(home)
    assert config.files_dir() == Path(home) / "files"
    assert config.db_path() == Path(home) / "chat.db"


def test_data_dir_defaults_to_home(monkeypatch):
    monkeypatch.delenv("AGENT_CHAT_HOME", raising=False)
    assert config.data_dir() == Path.home() / ".agent-chat"


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
