"""Antigravity CLI 1.5.10 transcript records, located by conversation UUID."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from . import Event
from ..wake.detector import WAKE_PREFIX

ROOT = Path.home() / ".gemini/antigravity-cli/brain"


def find_by_thread(thread: str, root: Path | None = None) -> Path | None:
    try:
        ident = str(uuid.UUID(thread))
    except (ValueError, TypeError, AttributeError):
        return None
    path = (root or ROOT) / ident / ".system_generated/logs/transcript.jsonl"
    return path if path.is_file() else None


def find_session(cwd: str) -> None:
    # NOTE: The transcript has no reliable cwd header. Never guess another conversation.
    return None


def parse(path: Path) -> list[Event]:
    events = []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            typ, source = row.get("type"), row.get("source")
            text = row.get("content")
            text = text if isinstance(text, str) else ""
            stamp = str(row.get("created_at") or "")
            if typ == "USER_INPUT" and source == "USER_EXPLICIT":
                if text.startswith("<USER_REQUEST>") and "</USER_REQUEST>" in text:
                    text = text[len("<USER_REQUEST>"):text.rfind("</USER_REQUEST>")].strip()
                if text:
                    events.append(Event("note" if text.startswith(WAKE_PREFIX) else "user", text, time=stamp))
            elif typ == "PLANNER_RESPONSE" and source == "MODEL":
                if text:
                    events.append(Event("assistant", text, time=stamp))
                calls = row.get("tool_calls")
                for call in calls if isinstance(calls, list) else []:
                    if isinstance(call, dict):
                        events.append(Event("tool_call", json.dumps(call.get("args", {}), ensure_ascii=False),
                                            name=str(call.get("name") or ""), time=stamp))
            elif typ == "GENERIC" and source == "MODEL" and text:
                # NOTE: Both native and MCP results use GENERIC, without stable call IDs.
                # Preserve their text; do not guess a tool name from parallel call order.
                events.append(Event("tool_result", text, time=stamp))
            elif typ == "ERROR_MESSAGE" and row.get("error"):
                events.append(Event("note", str(row["error"]), time=stamp))
    return events
