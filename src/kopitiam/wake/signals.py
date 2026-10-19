"""信号文件：适配器和 agent 那边的接收端（Claude 的 hook、pi 的扩展）之间的约定。

感知模块决定叫醒某个 agent 时，适配器把 {"id": 这次叫醒的编号, "text": 给 agent 的提示} 写进
wake/<kind>-<agent 进程号>.signal；接收端监视这个文件，读到新的 id 就把 text 交给 agent。
文件由接收端在 agent 启动时创建：文件在，就说明这个 agent 装好了接收端、能被叫醒。
「agent 进程号」和 MCP 进程认领身份时找到的 agent 进程（db.host_pid）是同一个，所以能对上。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..config import safe, wake_dir

TAKEN_MAX_AGE = 24 * 3600  # 去重标记留一天


def path(kind: str, pid: int) -> Path:
    return wake_dir() / f"{kind}-{pid}.signal"


def ready(kind: str, agent: dict) -> bool:
    return bool(agent.get("pid")) and path(kind, agent["pid"]).exists()


def write(kind: str, call, **extra) -> None:
    p = path(kind, call.pid)
    if not p.exists():
        raise RuntimeError("信号文件不见了（agent 可能已经关掉）")
    body = {"id": call.wake_id, "text": call.text, "upto": max(call.message_ids),
            "name": call.name, "senders": call.senders, "count": len(call.message_ids)} | extra
    p.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


def read(p: Path) -> dict | None:
    """读信号文件；赶上正在写的半截内容就稍等再读。空文件 = 没有要交的提示。"""
    for _ in range(10):
        try:
            raw = p.read_text(encoding="utf-8").strip()
            if not raw:
                return None
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) and obj.get("id") and obj.get("text") else None
        except ValueError:
            time.sleep(0.05)
        except OSError:
            return None
    return None


def take(wake_id: str) -> bool:
    """同一次叫醒只交一次：接收端可能被触发好几次，谁先建出标记谁算数。"""
    taken = wake_dir() / "taken"
    taken.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(taken / safe(str(wake_id)), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return False
    return True


def taken(wake_id: str) -> bool:
    return (wake_dir() / "taken" / safe(str(wake_id))).exists()


def cleanup() -> None:
    """删掉已经关掉的 agent 留下的信号文件、一天以前的去重标记。尽力而为。"""
    try:
        import psutil
        d = wake_dir()
        for f in d.glob("*-*.signal"):
            pid = f.stem.rpartition("-")[2]
            if pid.isdigit() and not psutil.pid_exists(int(pid)):
                f.unlink(missing_ok=True)
        for f in (d / "taken").glob("*"):
            if time.time() - f.stat().st_mtime > TAKEN_MAX_AGE:
                f.unlink(missing_ok=True)
    except Exception:
        pass
