"""所有测试共用：把数据目录指到临时目录，绝不碰真实的 ~/.agent-chat。

config.py 在函数里读取 AGENT_CHAT_HOME，所以 monkeypatch.setenv 之前之后都无所谓，
只要在第一次调用 data_dir() 之前设置好即可。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CHAT_HOME", str(tmp_path / "agent-chat-home"))
    yield
