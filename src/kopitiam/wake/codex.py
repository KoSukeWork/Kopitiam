"""Codex 适配器。

Codex 每次调用 kopitiam 的工具，都在 _meta.threadId 里带上当前对话 ID（MCP 进程记在 agents.thread_id），
所以知道「最近用过 kopitiam 的对话」是哪个。怎么把提示交给它，要看这个对话由谁托管：

- 桌面版（kopitiam 的 MCP 进程挂在桌面版自己的 app-server 下面）：把提示排进 Codex 的共享队列
  ~/.codex/queue_1.sqlite（和在桌面版里「排队」发一条一样）。桌面版自己会发现队列变了：对话闲着马上开始，
  正在忙就这一轮做完接着做。过一会儿还没被拿走（对话没加载），就打开 codex://threads/wake/<对话ID>，
  桌面版会加载那个对话、开始处理队列。自始至终只有桌面版一个进程在写这个对话。
  队列格式和验证过的不一样（Codex 更新过）就不碰它，退回下面的 Stop hook 接力。
- 共享的 app-server daemon（MCP 进程挂在 daemon 下面，codex-cli 0.163 的命令行窗口默认如此）：
  用 codex queue --thread 排一条消息，对话空闲也会马上开始。
- 命令行窗口自己内置的服务：外面进不去（打开唤醒链接会让桌面版另外加载同一个对话，两个进程同时写记录）。
  改成 Stop hook 接力：提示写进信号文件，那个对话这一轮结束时（kopitiam hook codex-stop）让它接着去 read。
  闲着的对话要等它下一轮做完。

SessionStart hook（kopitiam hook codex-start）建信号文件 wake/codex-<托管对话的 codex 进程号>.signal；
hook 和 kopitiam 的 MCP 进程都由这个 codex 进程启动，所以进程号对得上。
叫不醒、或者排进队列却一直没开始时，弹 Windows 通知提醒用户（remind）。为了知道对话是不是闲着，
UserPromptSubmit hook（codex-busy，后台运行）记「开始一轮」，Stop hook 不接力时记「这一轮结束」。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing
from pathlib import Path

from ..config import safe, wake_dir
from . import hookfile, notify, signals

HANDED_OFF = "已交给 Codex：它这一轮做完会接着读（闲着的话会弹通知提醒你）"
REMIND_IDLE = 3     # 对话闲着（hook 记下的状态）：交出去这么久还没被接走就提醒（秒）
REMIND_AFTER = 20   # 不知道对话忙不忙时（还没装记录状态的 hook）：交出去这么久还没被接走、
IDLE_FOR = 15       # 并且记录文件这么久没动，才当它闲着（秒）
STATE_STALE = 1800  # 「正在忙」记了这么久还没结束（比如 Codex 中途崩了）：不再相信它（秒）
WAKE_LINK_AFTER = 8  # 桌面版：排进队列这么久还没被拿走（对话多半没加载），就打开唤醒链接（秒）
REMIND_QUEUED = 45   # 打开唤醒链接以后还是没开始、对话也闲着：排进队列这么久以后弹通知（秒）

# 验证过的 Codex 队列格式（桌面版 26.1013 / codex-cli 0.163）：迁移版本 + queued_items 的列。对不上就不碰它。
QUEUE_VERSION = 2
QUEUE_COLUMNS = ["id", "thread_id", "payload_json", "queue_order", "created_at_ms", "updated_at_ms"]


def _exe(pid: int | None) -> str:
    try:
        import psutil
        return psutil.Process(pid).exe() if pid else ""
    except Exception:
        return ""


def hosted_by_daemon(pid: int | None) -> bool:
    """MCP 进程挂在共享 daemon 下面（daemon 装在 ~/.codex/packages/app-server-daemon/）。"""
    return "app-server-daemon" in _exe(pid).replace("\\", "/")


def hosted_by_desktop(pid: int | None) -> bool:
    """MCP 进程挂在桌面版自己的 app-server 下面：它的上一级是桌面版（WindowsApps\\OpenAI.Codex_…\\ChatGPT.exe）。"""
    try:
        import psutil
        parent = psutil.Process(pid).parent() if pid else None
        return parent is not None and "openai.codex_" in parent.exe().lower()
    except Exception:
        return False


def codex_home() -> Path:
    base = os.environ.get("CODEX_HOME")
    return Path(base) if base else Path.home() / ".codex"


# ---- 桌面版：Codex 的共享队列 ----

class QueueError(Exception):
    pass


def _queue() -> sqlite3.Connection:
    """打开 Codex 的队列；格式和验证过的不一样就报 QueueError（不建文件、不改格式）。"""
    p = codex_home() / "queue_1.sqlite"
    if not p.is_file():
        raise QueueError("没找到 Codex 的队列（queue_1.sqlite）")
    c = sqlite3.connect(f"{p.as_uri()}?mode=rw", uri=True, timeout=10)
    try:
        version = c.execute("SELECT MAX(version) FROM _sqlx_migrations WHERE success").fetchone()[0]
        cols = [r[1] for r in c.execute("PRAGMA table_info(queued_items)")]
    except sqlite3.Error as e:
        c.close()
        raise QueueError(f"读不了 Codex 的队列：{e}") from e
    if version != QUEUE_VERSION or cols != QUEUE_COLUMNS:
        c.close()
        raise QueueError(f"Codex 的队列格式变了（版本 {version}）")
    return c


def queue_problem() -> str | None:
    """队列能不能用：能用返回 None，不能用返回原因。"""
    try:
        _queue().close()
    except QueueError as e:
        return str(e)
    return None


def _client_id(wake_id: str) -> str:
    """同一次叫醒的消息编号固定（排过的不重复排）。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kopitiam:wake:{wake_id}"))


def _has(c: sqlite3.Connection, thread: str, wake_id: str) -> bool:
    return c.execute("SELECT 1 FROM queued_items WHERE thread_id = ? AND instr(payload_json, ?) > 0",
                     (thread, _client_id(wake_id))).fetchone() is not None


def enqueue(thread: str, text: str, wake_id: str) -> None:
    """给这个对话排一条消息，写法和 Codex 自己排队时一样（编号用按时间排序的 UUIDv7、排在最后）。"""
    payload = {"UserInput": {"content": [{"type": "text", "text": text, "text_elements": []}],
                             "client_id": _client_id(wake_id)}}
    now = int(time.time() * 1000)
    with closing(_queue()) as c, c:
        if _has(c, thread, wake_id):
            return
        c.execute(
            "INSERT INTO queued_items (id, thread_id, payload_json, queue_order, created_at_ms, updated_at_ms)"
            " VALUES (?, ?, ?, COALESCE((SELECT MAX(queue_order) FROM queued_items WHERE thread_id = ?), -1) + 1,"
            " ?, ?)",
            (str(uuid.uuid7()), thread, json.dumps(payload, ensure_ascii=False), thread, now, now),
        )


def queued(thread: str, wake_id: str) -> bool:
    """这条还在队列里（Codex 还没拿走）。"""
    with closing(_queue()) as c:
        return _has(c, thread, wake_id)


def _pending_path(agent_id) -> Path:
    return wake_dir() / "codex-queue" / f"{agent_id}.json"


# ---- 适配器（感知模块那边调用） ----

def check(agent: dict) -> str | None:
    if not agent.get("thread_id"):
        return "Codex 还没在对话里用过 kopitiam（先让要接收消息的那个对话调用一次 kopitiam，比如 whoami）"
    pid = agent.get("pid")
    if hosted_by_daemon(pid) or (hosted_by_desktop(pid) and queue_problem() is None) \
            or signals.ready("codex", agent):
        return None
    return "Codex 没装接力 hook（运行 kopitiam setup codex，然后重开 Codex）"


def hint(agent: dict) -> str | None:
    """能叫醒，但有限制：给网页显示。"""
    pid = agent.get("pid")
    if hosted_by_daemon(pid):
        return None
    if hosted_by_desktop(pid):
        problem = queue_problem()
        if problem is None:
            return None
        return f"{problem}，退回接力：忙的时候收到这一轮做完接着读，闲着的时候叫不醒"
    return "对话在命令行窗口自己里面：忙的时候收到，这一轮做完接着读；闲着的时候叫不醒（Codex 的限制）"


def wake(call) -> str | None:
    if hosted_by_daemon(call.pid):
        r = subprocess.run([_exe(call.pid), "queue", "--thread", call.thread_id, "--message", call.text],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:200] or f"codex queue 退出码 {r.returncode}")
        return None
    if hosted_by_desktop(call.pid):
        try:
            enqueue(call.thread_id, call.text, call.wake_id)
        except QueueError:
            pass  # 队列用不了：退回 Stop hook 接力
        else:
            p = _pending_path(call.agent_id)  # 给 remind() 看：多久没被拿走就打开唤醒链接
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"id": call.wake_id, "thread": call.thread_id, "pid": call.pid,
                                     "name": call.name, "senders": call.senders,
                                     "count": len(call.message_ids), "at": time.time()},
                                    ensure_ascii=False), encoding="utf-8")
            return None
    signals.write("codex", call, thread=call.thread_id)
    return HANDED_OFF


def _open_link(url: str) -> None:
    os.startfile(url)  # 只有 Windows 有；桌面版也只在 Windows 上走到这里


def remind() -> None:
    """感知模块的后台线程定时调用（只看几个小文件）。

    - 桌面版：排进队列的提示 WAKE_LINK_AFTER 秒还没被拿走（对话多半没加载），打开 codex://threads/wake/<对话ID>
      （只叫桌面版加载对话、处理队列，不切换页面）。还是没开始、对话也闲着，就弹通知。
    - 命令行窗口闲着叫不醒：交出去的提示一直没被接走、那个对话也没在动，就弹通知。

    点通知打开那个对话（codex://threads/<对话ID>，通知里写上对话标题）。每一批只提醒一次；
    同一个 agent 只留最新一条通知，提示被拿走以后把通知收掉，免得以后点开旧通知去了不相干的对话。
    """
    _remind_queued()
    _remind_handoffs()


def _remind_queued() -> None:
    import psutil

    for f in (wake_dir() / "codex-queue").glob("*.json"):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
            thread, wake_id, pid = obj["thread"], obj["id"], obj["pid"]
        except (OSError, ValueError, KeyError, TypeError):
            f.unlink(missing_ok=True)
            continue
        tag = f"codex-{f.stem}"
        try:
            waiting = queued(thread, wake_id)
        except QueueError:
            waiting = False  # 队列打不开了：没法再跟进
        if not waiting or not psutil.pid_exists(pid):
            # 拿走了；或者 Codex 关了（消息留在 Codex 的队列里，下次打开那个对话时它自己会处理）
            f.unlink(missing_ok=True)
            _clear_toast(tag)
            continue
        age = time.time() - float(obj.get("at") or 0)
        # 正在一轮里：对话肯定加载着，这一轮做完自己会拿，不用唤醒链接
        if age >= WAKE_LINK_AFTER and _busy(thread) is not True and signals.take(f"wakelink-{wake_id}"):
            _open_link(f"codex://threads/wake/{thread}")
        elif age >= REMIND_QUEUED and _idle(thread, age, REMIND_QUEUED) and signals.take(f"remind-{wake_id}"):
            _toast(obj, thread, tag, "已经排进它的队列，但一直没开始：")


def _remind_handoffs() -> None:
    import psutil

    for f in wake_dir().glob("codex-*.signal"):
        obj = signals.read(f)
        if not obj:
            continue
        tag = f"codex-{str(obj['id']).partition('-')[0]}"  # 叫醒编号开头是 agent 的 id
        if signals.taken(obj["id"]):
            _clear_toast(tag)
            continue
        pid = f.stem.rpartition("-")[2]
        if not pid.isdigit() or not psutil.pid_exists(int(pid)):
            continue  # Codex 已经关了
        thread = obj.get("thread")
        if _idle(thread, time.time() - f.stat().st_mtime, REMIND_IDLE) and signals.take(f"remind-{obj['id']}"):
            _toast(obj, thread, tag, "Codex 闲着时叫不醒：")


def _idle(thread: str | None, age: float, wait: float) -> bool:
    """交出去 age 秒了：那个对话闲着、而且等够了 wait 秒。

    正在一轮里不算（这一轮做完会接着处理）；不知道忙不忙时（没装记录状态的 hook）按记录文件有没有在动来猜，
    等久一点免得在它忙的时候打扰。
    """
    from ..export.codex import find_by_thread

    busy = _busy(thread) if thread else None
    if busy:
        return False
    if busy is None:
        rollout = find_by_thread(thread) if thread else None
        return age >= max(wait, REMIND_AFTER) and not (rollout and time.time() - rollout.stat().st_mtime < IDLE_FOR)
    return age >= wait


def _toast(obj: dict, thread: str | None, tag: str, why: str) -> None:
    from ..export.codex import thread_title

    who = "、".join(obj.get("senders") or []) or "有人"
    title = thread_title(thread) if thread else None
    where = f"「{title}」" if title else "最近用过 kopitiam 的那个对话"
    notify.toast(f"{obj.get('name') or 'codex'} 有新消息在等它",
                 f"{who} 发来 {obj.get('count') or 1} 条。{why}"
                 f"点这里打开{where}，随便跟它说一句，它这一轮做完就会去读。",
                 f"codex://threads/{thread}" if thread else "codex://", tag=tag)
    marker = wake_dir() / "taken" / f"toast-{tag}"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def _clear_toast(tag: str) -> None:
    """提示被拿走了：之前弹的通知没用了，收掉。"""
    marker = wake_dir() / "taken" / f"toast-{tag}"
    if marker.exists():
        marker.unlink(missing_ok=True)
        notify.remove(tag)


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
    return codex_home() / "hooks.json"


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
