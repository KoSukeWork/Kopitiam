"""codex（~/.codex/sessions/**/rollout-*.jsonl）解析器。

只用 type=="response_item" 的行；event_msg 里的内容和它重复，忽略。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import Event, find_by_cwd

ROOT = Path.home() / ".codex" / "sessions"
PATTERN = "**/rollout-*.jsonl"


def find_session(cwd: str, home: Path | None = None) -> Path | None:
    root = (home / ".codex" / "sessions") if home else ROOT
    return find_by_cwd(root, PATTERN, header_cwd, cwd)


def find_by_thread(thread_id: str, home: Path | None = None) -> Path | None:
    """记录文件名的结尾就是对话 ID：rollout-<时间>-<对话ID>.jsonl（归档的在 archived_sessions）。"""
    if not thread_id or not all(ch.isalnum() or ch == "-" for ch in thread_id):
        return None
    base = (home / ".codex") if home else ROOT.parent
    for root in (base / "sessions", base / "archived_sessions"):
        hits = sorted(root.glob(f"**/rollout-*-{thread_id}.jsonl")) if root.is_dir() else []
        if hits:
            return hits[-1]
    return None


def header_cwd(path: Path) -> str | None:
    with path.open(encoding="utf-8", errors="replace") as f:
        first = f.readline()
    try:
        obj = json.loads(first)
    except ValueError:
        return None
    if isinstance(obj, dict) and obj.get("type") == "session_meta":
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        cwd = payload.get("cwd")
        return str(cwd) if cwd else None
    return None


def parse(path: Path) -> list[Event]:
    events: list[Event] = []
    names: dict[str, str] = {}  # call_id → 工具名
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            typ = obj.get("type")
            ts = str(obj.get("timestamp") or "")
            if typ == "compacted":
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                events.append(Event(
                    "note", "（上下文被压缩，以下为摘要）\n" + str(payload.get("message") or ""),
                    time=ts,
                ))
                continue
            if typ != "response_item":
                continue  # session_meta / event_msg（内容重复）等都忽略
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            ptype = payload.get("type")
            if ptype == "message":
                _message(payload, ts, events)
            elif ptype == "reasoning":
                parts = []
                for item in payload.get("summary") or []:
                    if isinstance(item, dict) and item.get("text"):
                        parts.append(str(item["text"]))
                text = "\n".join(parts)  # encrypted_content 忽略
                if text:
                    events.append(Event("thinking", text, time=ts))
            elif ptype == "function_call":
                cid = str(payload.get("call_id") or "")
                name = str(payload.get("name") or "")
                if cid:
                    names[cid] = name
                text = str(payload.get("arguments") or "")
                if text:
                    events.append(Event("tool_call", text, name=name, time=ts))
            elif ptype == "custom_tool_call":
                cid = str(payload.get("call_id") or "")
                name = str(payload.get("name") or "")
                if cid:
                    names[cid] = name
                text = str(payload.get("input") or "")
                if text:
                    events.append(Event("tool_call", text, name=name, time=ts))
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                text = _output_text(payload.get("output"))
                if text:
                    events.append(Event(
                        "tool_result", text,
                        name=names.get(str(payload.get("call_id") or ""), ""),
                        time=ts,
                    ))
            # 其他 payload 类型跳过
    return events


def _message(payload: dict, ts: str, events: list[Event]) -> None:
    role = payload.get("role")
    content = payload.get("content")
    if role in ("developer", "system"):
        return
    if role == "user":
        parts: list[str] = []
        for block in _blocks(content):
            bt = block.get("type")
            if bt == "input_text":
                text = (block.get("text") or "").strip()
                # < 开头或 # AGENTS.md 开头的是系统自动注入的上下文
                if text.startswith("<") or text.startswith("# AGENTS.md"):
                    continue
                parts.append(text)
            elif bt == "input_image":
                parts.append("[图片]")
        text = "\n".join(p for p in parts if p)
        if text:
            events.append(Event("user", text, time=ts))
    elif role == "assistant":
        parts = [
            block.get("text") or ""
            for block in _blocks(content)
            if block.get("type") == "output_text"
        ]
        text = "\n".join(p for p in parts if p)
        if text:
            events.append(Event("assistant", text, time=ts))


def _blocks(content) -> list[dict]:
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, dict):
        return [content]
    return []


def _output_text(output) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts = [
            b.get("text") or ""
            for b in output
            if isinstance(b, dict) and b.get("type") == "input_text"
        ]
        return "\n".join(p for p in parts if p)
    return ""
