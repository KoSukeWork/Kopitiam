"""Claude Code 适配器：靠 Claude Code 自己的 hooks 叫醒，不需要 claude.ai 账号。

- SessionStart hook（kopitiam hook claude-start）：建信号文件 wake/claude-<CLAUDE_PID>.signal，
  返回 watchPaths，让 Claude Code 监视它。
- 感知模块决定叫醒它时，wake() 把这次的提示写进信号文件。
- FileChanged hook（kopitiam hook claude-wake，asyncRewake）：把提示写到 stderr，退出码 2——
  Claude Code 收到后，会话空闲也会马上开始处理。

CLAUDE_PID 是 Claude Code 给子进程设的环境变量，等于 MCP 进程认领身份时找到的 agent 进程（db.host_pid），
所以「agent 的 pid」就能对上「哪个信号文件」（见 signals.py）。hook 进程里只用标准库 + psutil，启动要快。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..config import norm, wake_dir
from . import hookfile, signals


def signal_path(pid: int) -> Path:
    return signals.path("claude", pid)


# ---- 适配器（感知模块那边调用） ----

def check(agent: dict) -> str | None:
    if signals.ready("claude", agent):
        return None
    return "Claude 没装唤醒 hook（运行 kopitiam setup claude，然后重开 Claude）"


def wake(call) -> None:
    signals.write("claude", call)


# ---- hook（Claude Code 那边调用） ----

def _claude_pid(env) -> int | None:
    v = env.get("CLAUDE_PID", "")
    if v.isdigit():
        return int(v)
    try:  # 老版本没有 CLAUDE_PID：往上找 claude 进程
        import psutil
        for p in psutil.Process().parents():
            if p.name().lower().startswith("claude"):
                return p.pid
    except Exception:
        pass
    return None


def hook_start(env=os.environ) -> str:
    """SessionStart：返回给 Claude Code 的 JSON（stdout）。拿不到 pid 就什么都不做。"""
    pid = _claude_pid(env)
    if pid is None:
        return ""
    wake_dir().mkdir(parents=True, exist_ok=True)
    signals.cleanup()
    sig = signal_path(pid)
    if not sig.exists():
        sig.write_text("", encoding="utf-8")
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "watchPaths": [str(sig)]}})


def hook_wake(stdin_text: str, env=os.environ) -> tuple[int, str]:
    """FileChanged：返回 (退出码, 给 Claude 看的文字)。2 = 叫醒；0 = 不是给我的 / 已经处理过。"""
    try:
        data = json.loads(stdin_text or "{}")
    except ValueError:
        return 0, ""
    if data.get("event") == "unlink":
        return 0, ""
    pid = _claude_pid(env)
    fp = data.get("file_path") or ""
    if pid is None or not fp or norm(fp) != norm(signal_path(pid)):
        return 0, ""  # 同一个会话里别的被监视的文件
    obj = signals.read(Path(fp))
    # 一次写入可能触发好几次 FileChanged：只交一次
    if not obj or not signals.take(obj["id"]):
        return 0, ""
    return 2, str(obj["text"])


# ---- 安装：把两个 hook 写进 ~/.claude/settings.json ----

def settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "settings.json"


def hook_entries(exe: str) -> dict:
    return {
        "SessionStart": {"hooks": [{"type": "command", "command": exe, "args": ["hook", "claude-start"]}]},
        "FileChanged": {"hooks": [{"type": "command", "command": exe, "args": ["hook", "claude-wake"],
                                   "asyncRewake": True}]},
    }


def _is_ours(group) -> bool:
    try:
        return any(h.get("args", [])[:1] == ["hook"] and Path(h["command"]).stem.lower() == "kopitiam"
                   for h in group.get("hooks", []))
    except (AttributeError, KeyError, TypeError):
        return False


def install(settings: Path, exe: str, *, remove: bool = False) -> str:
    """只增删 kopitiam 自己的两个 hook，不动别的；改之前先备份成 settings.json.bak。"""
    return hookfile.merge(settings, hook_entries(exe), _is_ours, remove=remove)
