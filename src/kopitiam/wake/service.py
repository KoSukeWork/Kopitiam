"""把感知模块和适配器连起来：每隔 WAKE_INTERVAL 秒感知一次，把要叫醒的交给对应的适配器。"""
from __future__ import annotations

import threading
import time
from contextlib import closing
from typing import Callable

from .. import db
from ..config import WAKE_INTERVAL, WAKE_MAX_ROUNDS
from . import claude, detector
from .detector import WakeCall

ADAPTERS = {"claude": claude}
HEARTBEAT = 10  # 每隔几秒在库里记一次「自动唤醒在运行」，网页据此提示有没有在跑


def check(agent: dict) -> str | None:
    mod = ADAPTERS.get(agent["kind"])
    if mod is None:
        return f"{agent['kind']} 还不支持自动唤醒"
    return mod.check(agent)


def run_once() -> list[tuple[WakeCall, str | None]]:
    """感知一轮并叫醒；返回 [(叫醒了谁, 失败原因或 None)]。"""
    results = []
    for call in detector.scan(check):
        try:
            ADAPTERS[call.kind].wake(call)
            err = None
        except Exception as e:
            err = str(e) or type(e).__name__
        detector.record(call, err)
        results.append((call, err))
    return results


def overview() -> dict:
    """网页用：在不在运行、总开关、来回上限，以及每个开着的 agent 能不能叫醒。"""
    try:
        running = time.time() - float(db.get_meta("wake_alive") or 0) < 3 * HEARTBEAT
    except ValueError:
        running = False
    st = detector.states()
    with closing(db.connect()) as c:
        rows = c.execute("SELECT * FROM agents WHERE kind NOT IN ('human', 'remote') ORDER BY id").fetchall()
    agents = {}
    for r in rows:
        if not db.is_online(r):
            continue  # 没开着的不会被叫醒，也不用显示
        s = st.get(r["id"], {})
        note, reason = s.get("note", ""), check(dict(r))
        supported = r["kind"] in ADAPTERS
        limited = "连续来回超过" in note
        agents[r["name"]] = {
            "kind": r["kind"], "enabled": s.get("enabled", True), "supported": supported,
            "reason": reason, "note": note, "limited": limited, "woken_at": s.get("woken_at"),
            # 要用户处理的情况：没装 hook、来回超过上限、叫醒失败（还没适配的 agent 不算）
            "warn": supported and bool(reason or limited or note.startswith("叫醒失败")),
        }
    return {"running": running, "paused": detector.paused(), "max_rounds": detector.max_rounds(),
            "default_rounds": WAKE_MAX_ROUNDS, "agents": agents}


class WakeService:
    """后台线程。on_event(call, error) 每叫醒一个 agent 调一次（kopitiam wake 用它打印日志）。"""

    def __init__(self, interval: float = WAKE_INTERVAL,
                 on_event: Callable[[WakeCall, str | None], None] | None = None):
        self.interval = interval
        self.on_event = on_event
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            db.set_meta("wake_alive", "0")  # 网页马上就能看到「没在运行」
        except Exception:
            pass

    def run_forever(self) -> None:
        """前台运行（kopitiam wake）：Ctrl+C 退出。"""
        self._loop()

    def _loop(self) -> None:
        beat = 0.0
        while not self._stop.wait(self.interval):
            try:
                if time.time() - beat >= HEARTBEAT:
                    db.set_meta("wake_alive", str(time.time()))
                    beat = time.time()
                for call, err in run_once():
                    if self.on_event:
                        self.on_event(call, err)
            except Exception:  # NOTE: 后台线程不能因为一次意外就退出（比如数据库正忙），下一轮再试
                pass
