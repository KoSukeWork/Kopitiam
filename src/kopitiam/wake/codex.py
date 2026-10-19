"""Codex 适配器。

Codex 每次调用 kopitiam 的工具，都在 _meta.threadId 里带上当前对话 ID（MCP 进程记在 agents.thread_id），
所以知道「最近用过 kopitiam 的对话」是哪个。怎么把提示交给它，要看这个对话由谁托管：

- 共享的 app-server daemon（kopitiam 的 MCP 进程就挂在 daemon 下面）：用 codex queue --thread 排一条消息，
  对话空闲也会马上开始。对话归 daemon 管，不会有两个进程同时写它。
- 桌面版 / 命令行窗口自己内置的服务（现在的默认情况）：外面进不去。codex queue 会让 daemon 另外加载
  同一个对话、两个进程同时写记录，所以不用。改成 Stop hook 接力：提示写进信号文件，
  那个对话这一轮结束时（kopitiam hook codex-stop）让它接着去 read。闲着的对话要等它下一轮做完。

SessionStart hook（kopitiam hook codex-start）建信号文件 wake/codex-<托管对话的 codex 进程号>.signal；
hook 和 kopitiam 的 MCP 进程都由这个 codex 进程启动，所以进程号对得上。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ..config import wake_dir
from . import hookfile, signals

HANDED_OFF = "已交给 Codex：它这一轮做完会接着读（闲着的话要等它下一轮）"


def _exe(pid: int | None) -> str:
    try:
        import psutil
        return psutil.Process(pid).exe() if pid else ""
    except Exception:
        return ""


def hosted_by_daemon(pid: int | None) -> bool:
    """MCP 进程挂在共享 daemon 下面（daemon 装在 ~/.codex/packages/app-server-daemon/）。"""
    return "app-server-daemon" in _exe(pid).replace("\\", "/")


# ---- 适配器（感知模块那边调用） ----

def check(agent: dict) -> str | None:
    if not agent.get("thread_id"):
        return "Codex 还没在对话里用过 kopitiam（先让要接收消息的那个对话调用一次 kopitiam，比如 whoami）"
    if hosted_by_daemon(agent.get("pid")) or signals.ready("codex", agent):
        return None
    return "Codex 没装接力 hook（运行 kopitiam setup codex，然后重开 Codex）"


def hint(agent: dict) -> str | None:
    """能叫醒，但有限制：给网页显示。"""
    if hosted_by_daemon(agent.get("pid")):
        return None
    return "忙的时候收到：这一轮做完接着读；闲着的时候叫不醒（Codex 的限制）"


def wake(call) -> str | None:
    if hosted_by_daemon(call.pid):
        r = subprocess.run([_exe(call.pid), "queue", "--thread", call.thread_id, "--message", call.text],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:200] or f"codex queue 退出码 {r.returncode}")
        return None
    signals.write("codex", call, thread=call.thread_id)
    return HANDED_OFF


# ---- hook（Codex 那边调用） ----

def _codex_pid() -> int | None:
    """往上找启动这个 hook 的 codex 进程（中间可能隔着 shell）。"""
    try:
        import psutil
        for p in psutil.Process().parents():
            if p.name().lower() in ("codex.exe", "codex"):
                return p.pid
    except Exception:
        pass
    return None


def hook_start() -> None:
    """SessionStart：建信号文件（文件在 = 装了接力 hook）。不往 stdout 写东西，免得变成 Codex 的上下文。"""
    pid = _codex_pid()
    if pid is None:
        return
    wake_dir().mkdir(parents=True, exist_ok=True)
    signals.cleanup()
    sig = signals.path("codex", pid)
    if not sig.exists():
        sig.write_text("", encoding="utf-8")


def _already_read(pid: int, upto) -> bool:
    """用户在这期间已经让它读过了（last_read 到了这批消息）：提示过期，不用再接力。"""
    if not isinstance(upto, int):
        return False
    from contextlib import closing

    from .. import db
    with closing(db.connect()) as c:
        row = c.execute("SELECT last_read FROM agents WHERE pid=? AND kind='codex'", (pid,)).fetchone()
    return row is not None and row["last_read"] >= upto


def hook_stop(stdin_text: str) -> str:
    """Stop：返回要写到 stdout 的内容。要它接着做就是 {"decision": "block", "reason": 提示}，否则空。"""
    try:
        data = json.loads(stdin_text or "{}")
    except ValueError:
        return ""
    pid = _codex_pid()
    obj = signals.read(signals.path("codex", pid)) if pid else None
    if not obj:
        return ""
    thread, session = obj.get("thread"), data.get("session_id")
    if thread and session and thread != session:
        return ""  # 同一个 Codex 里别的对话：留给用过 kopitiam 的那个
    if not signals.take(obj["id"]) or _already_read(pid, obj.get("upto")):
        return ""
    # ensure_ascii：Windows 管道默认不是 UTF-8，中文用 \u 转义最稳
    return json.dumps({"decision": "block", "reason": obj["text"]})


# ---- 安装：把两个 hook 写进 ~/.codex/hooks.json ----

def hooks_path() -> Path:
    base = os.environ.get("CODEX_HOME")
    return (Path(base) if base else Path.home() / ".codex") / "hooks.json"


def hook_entries(exe: str) -> dict:
    cmd = f'"{exe}"' if " " in exe else exe  # Codex 的 command 是一整行命令
    return {
        "SessionStart": {"hooks": [{"type": "command", "command": f"{cmd} hook codex-start", "timeout": 10}]},
        "Stop": {"hooks": [{"type": "command", "command": f"{cmd} hook codex-stop", "timeout": 10}]},
    }


def _is_ours(group) -> bool:
    try:
        return any("kopitiam" in h["command"].lower() and " hook codex-" in h["command"]
                   for h in group.get("hooks", []))
    except (AttributeError, KeyError, TypeError):
        return False


def install(path: Path, exe: str, *, remove: bool = False) -> str:
    """只增删 kopitiam 自己的两个 hook，不动别的；改之前先备份成 hooks.json.bak。"""
    return hookfile.merge(path, hook_entries(exe), _is_ours, remove=remove)
