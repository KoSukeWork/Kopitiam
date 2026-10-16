"""claude（~/.claude/projects/*/*.jsonl）解析器。

只处理顶层 type 为 user/assistant 的行；isSidechain / isMeta / <local-command 都跳过；
isCompactSummary 必须最先判断（这种行同时带 isVisibleInTranscriptOnly）。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import Event, find_by_cwd

ROOT = Path.home() / ".claude" / "projects"
PATTERN = "*/*.jsonl"  # 只取一层；子文件夹里是 subagent 的记录，不要


def find_session(cwd: str, home: Path | None = None) -> Path | None:
    root = (home / ".claude" / "projects") if home else ROOT
    return find_by_cwd(root, PATTERN, _header_cwd, cwd)


def _header_cwd(path: Path) -> str | None:
    """读前 50 行，第一个带 cwd 字段的对象。"""
    with path.open(encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i >= 50:
                break
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and "cwd" in obj:
                return str(obj["cwd"])
    return None


def parse(path: Path) -> list[Event]:
    events: list[Event] = []
    tool_names: dict[str, str] = {}  # tool_use id → 工具名
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue  # 坏行跳过
            if not isinstance(obj, dict):
                continue
            typ = obj.get("type")
            if typ not in ("user", "assistant"):
                continue  # mode / api-request* / attachment / file-history-snapshot … 都跳过
            # 最先判断：压缩摘要（必须在其他跳过规则之前）
            if typ == "user" and obj.get("isCompactSummary") is True:
                text = _text_of(obj.get("message", {}).get("content") if isinstance(obj.get("message"), dict) else None)
                events.append(Event("note", "（上下文被压缩，以下为摘要）\n" + text, time=str(obj.get("timestamp") or "")))
                continue
            if obj.get("isSidechain") is True or obj.get("isMeta") is True:
                continue
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            content = message.get("content")
            ts = str(obj.get("timestamp") or "")
            if typ == "user":
                _user(content, ts, events, tool_names)
            else:
                _assistant(content, ts, events, tool_names)
    return events


def _user(content, ts: str, events: list[Event], tool_names: dict) -> None:
    if isinstance(content, str):
        if content.startswith("<local-command"):
            return
        if content.startswith("<command-name>"):
            events.append(Event("note", f"命令：{_tag_inner(content, 'command-name')}", time=ts))
            return
        if content:
            events.append(Event("user", content, time=ts))
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt == "text":
            text = block.get("text") or ""
            if text:
                events.append(Event("user", text, time=ts))
        elif bt == "tool_result":
            text = _result_text(block.get("content"))
            if text:
                events.append(Event(
                    "tool_result", text,
                    name=tool_names.get(block.get("tool_use_id"), ""),
                    time=ts,
                ))
        # 其他块类型跳过


def _assistant(content, ts: str, events: list[Event], tool_names: dict) -> None:
    if isinstance(content, str):
        if content:
            events.append(Event("assistant", content, time=ts))
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt == "text":
            text = block.get("text") or ""
            if text:
                events.append(Event("assistant", text, time=ts))
        elif bt == "thinking":
            text = block.get("thinking") or ""
            if text:  # 很多 thinking 是加密的空字符串，跳过
                events.append(Event("thinking", text, time=ts))
        elif bt == "tool_use":
            bid = str(block.get("id") or "")
            name = str(block.get("name") or "")
            if bid:
                tool_names[bid] = name
            events.append(Event(
                "tool_call",
                json.dumps(block.get("input") or {}, ensure_ascii=False),
                name=name, time=ts,
            ))


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif b.get("type") == "image":
                parts.append("[图片]")
        return "\n".join(p for p in parts if p)
    return ""


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def _tag_inner(text: str, tag: str) -> str:
    start, end = f"<{tag}>", f"</{tag}>"
    if start in text and end in text:
        return text[text.index(start) + len(start):text.index(end)].strip()
    return text.strip()
