"""感知模块：看有没有点名发给某个开着的 agent 的新消息，按规则决定叫醒谁。

只读写数据库，不管怎么叫醒——那是适配器的事（wake/<kind>.py）。规则：
1. 只有点名发给它的消息才叫醒；发给所有人的广播不叫醒。
2. 用户发的（包括转发的）总是叫醒；agent 发给 agent 的，两个 agent 之间连续来回超过上限就不叫醒
   （消息照常送到）。用户对其中任何一个说话、或者两个 agent 之间 WAKE_CHAIN_IDLE 没说话，就重新计数。
3. 叫醒以后，它读到那一批之前，再来新消息不重复叫。
4. 没开着的 agent 不管；暂停期间和刚开始感知时的旧消息不补叫。
"""
from __future__ import annotations

import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .. import db
from ..config import WAKE_CHAIN_IDLE, WAKE_MAX_ROUNDS


@dataclass
class WakeCall:
    """这一轮要叫醒的一个 agent。"""
    agent_id: int
    name: str
    kind: str
    pid: int | None
    message_ids: list[int]
    senders: list[str]
    prev_woken_upto: int = 0  # 叫醒失败时恢复成这个值，下一条新消息还能再叫
    text: str = field(default="")

    @property
    def wake_id(self) -> str:
        """这一次叫醒的唯一编号，适配器那边用来去重（同一次叫醒只生效一次）。"""
        return f"{self.agent_id}-{max(self.message_ids)}"


def paused() -> bool:
    return db.get_meta("wake_paused") == "1"


def set_paused(value: bool) -> None:
    db.set_meta("wake_paused", "1" if value else "0")


def max_rounds() -> int:
    try:
        return max(0, int(db.get_meta("wake_max_rounds") or WAKE_MAX_ROUNDS))
    except ValueError:
        return WAKE_MAX_ROUNDS


def set_max_rounds(n: int) -> None:
    if not 0 <= n <= 100:
        raise ValueError("来回上限要在 0～100 之间（0 = agent 之间不叫醒）")
    db.set_meta("wake_max_rounds", str(n))


def wake_text(senders: list[str], count: int) -> str:
    who = "、".join(senders)
    return (f"kopitiam：你有 {count} 条新消息（来自 {who}）。请调用 kopitiam 的 read 查看，"
            "按内容处理；需要回复就用 send 发给对方。")


def _targets(to_ids: str) -> list[int]:
    return db.Chat._targets(to_ids)


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def chain_len(c, a: int, b: int, upto: int, user: int, idle: float = WAKE_CHAIN_IDLE) -> int:
    """到 upto 为止，a 和 b 之间连续互发了几条（点名互发的才算）。

    往回数：遇到用户对 a 或 b 说话（含广播）就停；相邻两条隔了 idle 秒以上也停。
    """
    rows = c.execute(
        "SELECT id, sender_id, to_ids, created_at FROM messages"
        " WHERE id <= ? AND sender_id IN (?, ?, ?) ORDER BY id DESC LIMIT 500",
        (upto, a, b, user),
    ).fetchall()
    n = 0
    prev: float | None = None
    for r in rows:
        t = _targets(r["to_ids"])
        if r["sender_id"] == user:
            if not t or a in t or b in t:
                break
            continue
        other = b if r["sender_id"] == a else a
        if other not in t:
            continue  # a 或 b 发给别人、或者广播：不算，也不打断
        ts = _ts(r["created_at"])
        if prev is not None and prev - ts > idle:
            break
        n += 1
        prev = ts
    return n


def scan(check: Callable[[dict], str | None]) -> list[WakeCall]:
    """一轮感知。返回这一轮要叫醒的 agent，并在库里记下（同一批不会再叫第二次）。

    check(agent) 由适配器提供：能叫醒返回 None，叫不醒返回原因（记在 note 里给网页显示）。
    """
    db.init_db()
    user = db.user_id()
    limit = max_rounds()
    stop = paused()
    calls: list[WakeCall] = []
    with closing(db.connect()) as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            top = c.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
            names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM agents")}
            agents = c.execute(
                "SELECT a.*, w.agent_id AS w_id, w.enabled, w.seen_upto, w.woken_upto"
                " FROM agents a LEFT JOIN wake_state w ON w.agent_id = a.id"
                " WHERE a.kind NOT IN ('human', 'remote')"
            ).fetchall()
            for a in agents:
                if a["w_id"] is None:
                    # 第一次见到：从现在开始算，以前的消息不补叫
                    c.execute("INSERT INTO wake_state(agent_id, seen_upto) VALUES(?, ?)", (a["id"], top))
                    continue
                start = max(a["seen_upto"], a["last_read"])
                if start >= top:
                    continue
                c.execute("UPDATE wake_state SET seen_upto=? WHERE agent_id=?", (top, a["id"]))
                if stop or not a["enabled"] or not db.is_online(a):
                    continue  # 这期间的消息以后也不补叫
                rows = c.execute(
                    "SELECT id, sender_id, to_ids FROM messages WHERE id > ? AND sender_id <> ? ORDER BY id",
                    (start, a["id"]),
                ).fetchall()
                pending = [r for r in rows if a["id"] in _targets(r["to_ids"])]
                if not pending:
                    continue
                ok = [r for r in pending
                      if r["sender_id"] == user
                      or chain_len(c, a["id"], r["sender_id"], r["id"], user) <= 2 * limit]
                if not ok:
                    who = "、".join(dict.fromkeys(names.get(r["sender_id"], "?") for r in pending))
                    note = (f"和 {who} 连续来回超过 {limit} 次，没有叫醒（你说一句话就重新计数）" if limit
                            else f"{who} 发来的没有叫醒（来回上限是 0：只有你发的消息才叫醒 agent）")
                    c.execute("UPDATE wake_state SET note=? WHERE agent_id=?", (note, a["id"]))
                    continue
                if a["woken_upto"] > a["last_read"]:
                    continue  # 上次叫醒的那一批还没读，不重复叫
                why = check(dict(a))
                if why:
                    c.execute("UPDATE wake_state SET note=? WHERE agent_id=?", (why, a["id"]))
                    continue
                senders = list(dict.fromkeys(names.get(r["sender_id"], "?") for r in ok))
                call = WakeCall(a["id"], a["name"], a["kind"], a["pid"], [r["id"] for r in ok], senders,
                                prev_woken_upto=a["woken_upto"])
                call.text = wake_text(senders, len(ok))
                c.execute("UPDATE wake_state SET woken_upto=? WHERE agent_id=?", (max(call.message_ids), a["id"]))
                calls.append(call)
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
    return calls


def record(call: WakeCall, error: str | None) -> None:
    """适配器叫完以后记结果；失败时恢复 woken_upto，下一条新消息还能再叫。"""
    with closing(db.connect()) as c:
        if error:
            c.execute("UPDATE wake_state SET woken_upto=?, note=? WHERE agent_id=? AND woken_upto=?",
                      (call.prev_woken_upto, f"叫醒失败：{error}", call.agent_id, max(call.message_ids)))
        else:
            c.execute("UPDATE wake_state SET woken_at=?, note=? WHERE agent_id=?",
                      (time.time(), f"已叫醒（{'、'.join(call.senders)} 的 {len(call.message_ids)} 条）",
                       call.agent_id))


def set_enabled(agent_id: int, value: bool) -> None:
    with closing(db.connect()) as c:
        c.execute("INSERT INTO wake_state(agent_id, enabled, seen_upto)"
                  " VALUES(?, ?, (SELECT COALESCE(MAX(id), 0) FROM messages))"
                  " ON CONFLICT(agent_id) DO UPDATE SET enabled=excluded.enabled",
                  (agent_id, 1 if value else 0))


def states() -> dict[int, dict]:
    """agent id → {enabled, note, woken_at}，给网页显示。"""
    with closing(db.connect()) as c:
        return {r["agent_id"]: {"enabled": bool(r["enabled"]), "note": r["note"] or "", "woken_at": r["woken_at"]}
                for r in c.execute("SELECT * FROM wake_state")}

