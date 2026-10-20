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
WHO_WINDOW = 7 * 24 * 3600         # 离线超过 7 天：who 和网页主列表不再显示，网页收进「已隐藏」（秒）
PURGE_AFTER = 30 * 24 * 3600       # 离线超过 30 天、且没有任何消息关联的身份：启动时自动删除（秒）

# 默认端口：kopi 在手机键盘上是 5674。以前用 8765/8766（Python websockets 文档的示例端口，很多工具都用，
# 代理软件 Clash 也会占 8765），换成不常见、又在 Windows 动态端口段（49152 起）以下的
WEB_PORT = 25674                   # 本机网页
# 团队（跨电脑，人和人之间互发消息）
PEER_PORT = 25675                  # 团队端口默认值（只开 /peer/* 接口，本机网页仍只给 127.0.0.1）
PEER_BODY_MAX = 64 * 1024          # /peer/message 等 JSON 请求体上限（字节）
PAIR_CODE_TTL = 10 * 60            # 配对码有效期（秒）
PAIR_MAX_TRIES = 5                 # 配对码最多试几次，超过就作废
PEER_CLOCK_SKEW = 300              # 签名里的时间戳允许误差（秒），超出拒绝，防重放
PEER_ONLINE_WINDOW = 90            # 最近一次成功联系在这个时间内，就算对方在线（秒）
FORWARD_EXTRA = 400                # 转发时正文可以比 TEXT_MAX 多出的长度（放「转发自…」那一行）

# 自动唤醒（wake/）：有人点名发消息给开着的 agent 时，叫它自己去 read
WAKE_INTERVAL = 1.5                # 感知模块多久查一次新消息（秒）
WAKE_MAX_ROUNDS = 6                # 两个 agent 之间自动唤醒的连续来回上限（默认值，网页上可以改；0 = agent 之间不叫醒）
WAKE_CHAIN_IDLE = 30 * 60          # 两个 agent 之间这么久没说话，来回次数重新计数（秒）
WAKE_RETRY = 10 * 60               # 叫醒以后这么久还没读，就再交一次（接收端按叫醒编号去重，送到过的不会重复）（秒）

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


def wake_dir() -> Path:
    """唤醒信号文件（claude-<pid>.signal 等）放在这里，agent 那边的 hook / 扩展监视它们。"""
    return data_dir() / "wake"


def staging_dir() -> Path:
    """别人发来的附件先落在这里，整条消息收齐、校验通过后再移进 FILES_DIR。"""
    return data_dir() / "peer-staging"


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
