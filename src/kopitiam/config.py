"""共享常量与路径工具。

所有文件读写都必须显式 encoding="utf-8"；读 agent 记录时另加 errors="replace"。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

DB_NAME = "chat.db"
FILES_SUBDIR = "files"

TEXT_MAX = 4000                    # 单条消息正文上限（字符）
FILE_MAX = 20 * 1024 * 1024        # 单个附件上限（字节）
FILES_MAX = 10                     # 单条消息附件个数上限
WHO_WINDOW = 7 * 24 * 3600         # who 列表里离线超过 7 天的不再显示（秒）

# 名字规则：1-32 个字符，\w（含中文）、点、@、横线；不分大小写唯一。
NAME_RE = re.compile(r"^[\w.@\-]{1,32}$")
RESERVED_NAMES = frozenset({"all", "所有人"})

# 导出截短上限（字）。clean 模式 thinking 直接不输出，用 None 表示。
TRUNC: dict[str, dict[str, int | None]] = {
    "clean": {"thinking": None, "tool_call": 300, "tool_result": 500},
    "full": {"thinking": 2000, "tool_call": 5000, "tool_result": 20000},
}
EXPORT_MAX = {"clean": 2 * 1024 * 1024, "full": 20 * 1024 * 1024}  # 导出总长上限（字节）


def data_dir() -> Path:
    """DATA_DIR。每次调用都读环境变量，测试用 KOPITIAM_HOME 覆盖。"""
    env = os.environ.get("KOPITIAM_HOME")
    if env:
        return Path(env)
    return Path.home() / ".kopitiam"


def files_dir() -> Path:
    return data_dir() / FILES_SUBDIR


def db_path() -> Path:
    return data_dir() / DB_NAME


def norm(p: str | os.PathLike) -> str:
    """统一路径比较用；数据库里存的仍然是 str(Path(p).resolve()) 原始写法。"""
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(p))))


def safe(name: str) -> str:
    """把任意文件名变成能放进 FILES_DIR 的安全名字：坏字符换 _，最长 100 字符且保留扩展名。"""
    cleaned = "".join("_" if (c in '<>:"/\\|?*' or ord(c) < 32) else c for c in name)
    if len(cleaned) > 100:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and ext and len(ext) < 100:
            cleaned = stem[: 100 - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:100]
    return cleaned or "_"


def valid_name(name: str) -> bool:
    """名字规则检查（不查唯一性）。"""
    return bool(NAME_RE.match(name)) and name.lower() not in RESERVED_NAMES
