"""所有测试共用：把数据目录指到临时目录，绝不碰真实的 ~/.kopitiam。

# NOTE: 不用 pytest 自带的 tmp_path fixture——本机 DSH 沙箱下子进程的目录 scandir/
# mkdir 行为不可靠，pytest 的 tmp_path 工厂会因此挂掉；这里直接在系统临时目录里建
# 唯一子目录，行为等价且稳定。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def home(monkeypatch):
    """每个测试一个独立的 KOPITIAM_HOME（函数里读环境变量，随时生效）。"""
    root = Path(tempfile.gettempdir()) / f"kopitiam-test-{uuid.uuid4().hex[:10]}"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KOPITIAM_HOME", str(root))
    yield root
    monkeypatch.delenv("KOPITIAM_HOME", raising=False)
    shutil.rmtree(root, ignore_errors=True)
