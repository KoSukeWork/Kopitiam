"""pi（~/.pi/agent/sessions/*/*.jsonl）解析器。

pi 的记录是树（每条都有 id/parentId），用户回退会产生分支，只导出当前分支：
叶子 = 文件里最后一个 type 为 message/compaction 的行，沿 parentId 走到根再反转。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import Event, find_by_cwd

ROOT = Path.home() / ".pi" / "agent" / "sessions"
PATTERN = "*/*.jsonl"


def find_session(cwd: str, home: Path | None = None) -> Path | None:
    root = (home / ".pi" / "agent" / "sessions") if home else ROOT
    return find_by_cwd(root, PATTERN, _header_cwd, cwd)


def _header_cwd(path: Path) -> str | None:
    with path.open(encoding="utf-8", errors="replace") as f:
        first = f.readline()
    try:
        obj = json.loads(first)
    except ValueError:
        return None
    if isinstance(obj, dict) and obj.get("type") == "session":
        cwd = obj.get("cwd")
        return str(cwd) if cwd else None
    return None


def parse(path: Path) -> list[Event]:
    nodes: dict[str, dict] = {}
    order: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("id") is not None:
                nodes[str(obj["id"])] = obj
                order.append(str(obj["id"]))

    leaf = ""
    for key in reversed(order):
        if nodes[key].get("type") in ("message", "compaction"):
            leaf = key
            break
    if not leaf:
        return []

    chain: list[dict] = []
    visited: set[str] = set()
    cur: str | None = leaf
    while cur and cur in nodes and cur not in visited:
        visited.add(cur)
        chain.append(nodes[cur])
        cur = nodes[cur].get("parentId")
        if cur is not None:
            cur = str(cur)
    chain.reverse()

    events: list[Event] = []
    names: dict[str, str] = {}  # toolCall id → 工具名
    for node in chain:
        typ = node.get("type")
        ts = str(node.get("timestamp") or "")
        if typ == "compaction":
            events.append(Event(
                "note", "（上下文被压缩，以下为摘要）\n" + str(node.get("summary") or ""),
                time=ts,
            ))
            continue
        if typ != "message":
            continue  # model_change / custom / context_edit / session_info … 只是树节点
        msg = node.get("message") if isinstance(node.get("message"), dict) else {}
        role = msg.get("role")
        if role == "system":
            continue
        if role == "user":
            text = _content_text(msg.get("content"))
            if text:
                events.append(Event("user", text, time=ts))
        elif role == "assistant":
            for block in _blocks(msg.get("content")):
                bt = block.get("type")
                if bt == "text":
                    text = block.get("text") or ""
                    if text:
                        events.append(Event("assistant", text, time=ts))
                elif bt == "thinking":
                    text = block.get("thinking") or ""
                    if text:
                        events.append(Event("thinking", text, time=ts))
                elif bt == "toolCall":
                    tid = str(block.get("id") or "")
                    name = str(block.get("name") or "")
                    if tid:
                        names[tid] = name
                    events.append(Event(
                        "tool_call",
                        json.dumps(block.get("arguments") or {}, ensure_ascii=False),
                        name=name, time=ts,
                    ))
        elif role == "toolResult":
            text = _content_text(msg.get("content"))
            if text:
                events.append(Event("tool_result", text, name=str(msg.get("toolName") or ""), time=ts))
        elif role == "bashExecution":
            # 用户在 pi 里用 ! 跑的命令
            text = f"$ {msg.get('command') or ''}\n{msg.get('output') or ''}"
            events.append(Event("tool_result", text, name="bash", time=ts))
        # 其他 role 跳过
    return events


def _blocks(content) -> list[dict]:
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _content_text(content) -> str:
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
