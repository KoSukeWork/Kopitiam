"""Bounded activity with stable event IDs and a cursor for merged text."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import threading


class Activity:
    def __init__(self, turns=3, events=500):
        self.turn_limit, self.event_limit = turns, events
        self.turns = deque()
        self.sequence = 0
        self.dropped = 0
        self._lock = threading.RLock()

    def new_turn(self):
        with self._lock:
            self.sequence += 1
            self.turns.append({"id": self.sequence, "events": deque()})
            while len(self.turns) > self.turn_limit:
                old = self.turns.popleft()
                self.dropped = max(self.dropped, old["id"], *(e["seq"] for e in old["events"]))

    def append(self, update):
        with self._lock:
            if not self.turns:
                self.new_turn()
            self.sequence += 1
            events = self.turns[-1]["events"]
            update = deepcopy(update)
            kind = update.get("sessionUpdate")
            text = update.get("content", {}).get("text") if isinstance(update.get("content"), dict) else None
            if (kind in {"agent_message_chunk", "agent_thought_chunk", "user_message_chunk"}
                    and isinstance(text, str)):
                update["content"]["text"] = text[-64000:]
                if events and events[-1]["update"].get("sessionUpdate") == kind:
                    previous = events[-1]["update"].get("content", {})
                    if previous.get("type") == "text":
                        previous["text"] = (previous.get("text", "") + text)[-64000:]
                        events[-1]["seq"] = self.sequence
                        return
            events.append({"id": self.sequence, "seq": self.sequence,
                           "turn": self.turns[-1]["id"], "update": update})
            while len(events) > self.event_limit:
                self.dropped = max(self.dropped, events.popleft()["seq"])

    def snapshot(self, after=0):
        with self._lock:
            reset = after > self.sequence or 0 < after < self.dropped
            return {"seq": self.sequence, "reset": reset,
                    "events": deepcopy([e for turn in self.turns for e in turn["events"]
                                        if reset or e["seq"] > after])}
