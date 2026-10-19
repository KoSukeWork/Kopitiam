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
闲着的对话叫不醒，就弹 Windows 通知提醒用户（remind）。为了知道对话是不是闲着，
UserPromptSubmit hook（codex-busy，后台运行）记「开始一轮」，Stop hook 不接力时记「这一轮结束」。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from ..config import safe, wake_dir
from . import hookfile, notify, signals

HANDED_OFF = "已交给 Codex：它这一轮做完会接着读（闲着的话会弹通知提醒你）"
REMIND_IDLE = 3     # 对话闲着（hook 记下的状态）：交出去这么久还没被接走就提醒（秒）
REMIND_AFTER = 20   # 不知道对话忙不忙时（还没装记录状态的 hook）：交出去这么久还没被接走、
IDLE_FOR = 15       # 并且记录文件这么久没动，才当它闲着（秒）
STATE_STALE = 1800  # 「正在忙」记了这么久还没结束（比如 Codex 中途崩了）：不再相信它（秒）


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


def remind() -> None:
    """Codex 闲着时叫不醒：交出去的提示一直没被接走、那个对话也没在动，就弹 Windows 通知提醒用户。

    点通知打开那个对话（codex://threads/<对话ID>，通知里写上对话标题）；用户随便跟它说一句，
    这一轮结束时 Stop hook 就会接力。每一批只提醒一次；同一个 agent 只留最新一条通知，
    提示被接走以后把通知收掉，免得以后点开旧通知去了不相干的对话。感知模块的后台线程定时调用。
    """
    import psutil

    from ..export.codex import find_by_thread, thread_title

    now = time.time()
    for f in wake_dir().glob("codex-*.signal"):
        obj = signals.read(f)
        if not obj:
            continue
        tag = f"codex-{str(obj['id']).partition('-')[0]}"  # 叫醒编号开头是 agent 的 id
        shown = wake_dir() / "taken" / f"toast-{tag}"
        if signals.taken(obj["id"]):
            if shown.exists():
                shown.unlink(missing_ok=True)
                notify.remove(tag)
            continue
        age = now - f.stat().st_mtime
        pid = f.stem.rpartition("-")[2]
        if not pid.isdigit() or not psutil.pid_exists(int(pid)):
            continue  # Codex 已经关了
        thread = obj.get("thread")
        busy = _busy(thread) if thread else None
        if busy:
            continue  # 正在一轮里：这一轮做完 Stop hook 会接力
        if busy is None:  # 不知道忙不忙：按记录文件有没有在动来猜，等久一点免得在它忙的时候打扰
            rollout = find_by_thread(thread) if thread else None
            if age < REMIND_AFTER or (rollout and now - rollout.stat().st_mtime < IDLE_FOR):
                continue
        elif age < REMIND_IDLE:
            continue
        if not signals.take(f"remind-{obj['id']}"):
            continue  # 这一批已经提醒过
        who = "、".join(obj.get("senders") or []) or "有人"
        title = thread_title(thread) if thread else None
        where = f"「{title}」" if title else "最近用过 kopitiam 的那个对话"
        notify.toast(f"{obj.get('name') or 'codex'} 有新消息在等它",
                     f"{who} 发来 {obj.get('count') or 1} 条。Codex 闲着时叫不醒："
                     f"点这里打开{where}，随便跟它说一句，它这一轮做完就会去读。",
                     f"codex://threads/{thread}" if thread else "codex://", tag=tag)
        shown.parent.mkdir(parents=True, exist_ok=True)
        shown.touch()


# ---- 每个对话忙不忙：UserPromptSubmit = 开始一轮，Stop 不接力 = 这一轮结束 ----

def _state_path(thread: str) -> Path:
    return wake_dir() / "codex-state" / f"{safe(thread)}.json"


def _set_busy(thread: str | None, busy: bool) -> None:
    if not thread:
        return
    p = _state_path(thread)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"busy": busy, "at": time.time()}), encoding="utf-8")


def _busy(thread: str) -> bool | None:
    """True = 正在一轮里；False = 闲着；None = 不知道（没装新版 hook，或者「正在忙」记得太久不可信）。"""
    try:
        st = json.loads(_state_path(thread).read_text(encoding="utf-8"))
        busy, at = bool(st["busy"]), float(st["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if busy and time.time() - at > STATE_STALE:
        return None
    return busy


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
    for f in (wake_dir() / "codex-state").glob("*.json"):  # 一天以前的对话状态没用了
        try:
            if time.time() - f.stat().st_mtime > signals.TAKEN_MAX_AGE:
                f.unlink()
        except OSError:
            pass
    sig = signals.path("codex", pid)
    if not sig.exists():
        sig.write_text("", encoding="utf-8")


def hook_busy(stdin_text: str) -> None:
    """UserPromptSubmit（后台运行，不拖慢你发消息）：这个对话开始了新的一轮。"""
    try:
        data = json.loads(stdin_text or "{}")
    except ValueError:
        return
    _set_busy(data.get("session_id"), True)


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
    session = data.get("session_id")
    out = _handoff(session)
    _set_busy(session, bool(out))  # 接力 = 接着忙；不接力 = 这一轮结束，闲着了
    return out


def _handoff(session: str | None) -> str:
    pid = _codex_pid()
    obj = signals.read(signals.path("codex", pid)) if pid else None
    if not obj:
        return ""
    thread = obj.get("thread")
    if thread and session and thread != session:
        return ""  # 同一个 Codex 里别的对话：留给用过 kopitiam 的那个
    if not signals.take(obj["id"]) or _already_read(pid, obj.get("upto")):
        return ""
    # ensure_ascii：Windows 管道默认不是 UTF-8，中文用 \u 转义最稳
    return json.dumps({"decision": "block", "reason": obj["text"]})


# ---- 安装：把三个 hook 写进 ~/.codex/hooks.json ----

def hooks_path() -> Path:
    base = os.environ.get("CODEX_HOME")
    return (Path(base) if base else Path.home() / ".codex") / "hooks.json"


def hook_entries(exe: str) -> dict:
    cmd = f'"{exe}"' if " " in exe else exe  # Codex 的 command 是一整行命令
    return {
        "SessionStart": {"hooks": [{"type": "command", "command": f"{cmd} hook codex-start", "timeout": 10}]},
        "UserPromptSubmit": {"hooks": [{"type": "command", "command": f"{cmd} hook codex-busy", "timeout": 10,
                                        "async": True}]},
        "Stop": {"hooks": [{"type": "command", "command": f"{cmd} hook codex-stop", "timeout": 10}]},
    }


def _is_ours(group) -> bool:
    try:
        return any("kopitiam" in h["command"].lower() and " hook codex-" in h["command"]
                   for h in group.get("hooks", []))
    except (AttributeError, KeyError, TypeError):
        return False


def install(path: Path, exe: str, *, remove: bool = False) -> str:
    """只增删 kopitiam 自己的几个 hook，不动别的；改之前先备份成 hooks.json.bak。
    顺便登记 Windows 通知的发送者名字（Codex 闲着时靠通知提醒用户）。"""
    done = hookfile.merge(path, hook_entries(exe), _is_ours, remove=remove)
    if remove:
        notify.unregister()
    else:
        notify.register()
    return done
