"""dsh（~/.dsh/sessions/*/*/session*.jsonl.zstd）解析器。

记录整体用 zstd 压缩（Python 3.14 标准库 compression.zstd），先解压再按行解析。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from compression import zstd

from . import Event, find_by_cwd

ROOT = Path.home() / ".dsh" / "sessions"
PATTERN = "*/*/session*.jsonl.zstd"


def _text(path: Path) -> str:
    """逐帧解压。dsh 正在写入的会话最后一帧可能不完整：保留已解出的部分，
    并丢掉最后一行可能被截断的半行，而不是整个文件解压失败。"""
    data = path.read_bytes()
    out: list[bytes] = []
    truncated = False
    while data:
        d = zstd.ZstdDecompressor()
        try:
            out.append(d.decompress(data))
        except zstd.ZstdError:
            truncated = True
            break
        if not d.eof:
            truncated = True
            break
        data = d.unused_data
    text = b"".join(out).decode("utf-8", "replace")
    if truncated and not text.endswith("\n"):
        text = text.rpartition("\n")[0]
    return text


def find_session(cwd: str, home: Path | None = None) -> Path | None:
    root = (home / ".dsh" / "sessions") if home else ROOT
    return find_by_cwd(root, PATTERN, _header_cwd, cwd)


def find_by_thread(session_id: str, home: Path | None = None) -> Path | None:
    """按会话 ID 找记录：~/.dsh/sessions/<工作目录>/<会话 ID>/session*.jsonl.zstd（会话 ID 就是那一层文件夹名）。"""
    if not session_id or not all(ch.isalnum() or ch == "-" for ch in session_id):
        return None
    root = (home / ".dsh" / "sessions") if home else ROOT
    hits = sorted(root.glob(f"*/{session_id}/session*.jsonl.zstd")) if root.is_dir() else []
    return hits[-1] if hits else None


def header_cwd(path: Path) -> str | None:
    return _header_cwd(path)


def _header_cwd(path: Path) -> str | None:
    lines = _text(path).splitlines()
    if not lines:
        return None
    try:
        obj = json.loads(lines[0])
    except ValueError:
        return None
    if isinstance(obj, dict) and obj.get("type") == "session":
        cwd = obj.get("cwd")
        return str(cwd) if cwd else None
    return None


def parse(path: Path) -> list[Event]:
    events: list[Event] = []
    names: dict[str, str] = {}  # 调用 id → 工具名
    for line in _text(path).splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        surface = obj.get("surfaceOp")
        if surface is not None and surface != "append":
            # NOTE: 目前没见过别的值，不知道是什么意思，先跳过
            continue
        typ = obj.get("type")
        if typ == "user/message":
            data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
            source = (data.get("source") or {}).get("kind") if isinstance(data.get("source"), dict) else None
            ts = _ms_iso(obj.get("time"))
            if source == "user":
                text = _content_text(data.get("content"))
                if text:
                    events.append(Event("user", text, time=ts))
            elif source in ("agent-message", "subagent-settled"):
                text = _content_text(data.get("content"))
                if text:
                    events.append(Event("note", _short(text, 300), time=ts))
            # 其他 kind（runtime-context / skill-catalog / goal / agent-instructions…）是注入的，跳过
        elif typ == "assistant/message":
            data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
            msg = data.get("message") if isinstance(data.get("message"), dict) else {}
            ts = _ms_iso(obj.get("time"))
            for block in _blocks(msg.get("content")):
                bt = block.get("type")
                if bt == "reasoning":
                    text = block.get("text") or ""
                    if text:
                        events.append(Event("thinking", text, time=ts))
                elif bt == "text":
                    text = block.get("text") or ""
                    if text:
                        events.append(Event("assistant", text, time=ts))
                elif bt == "tool-call":
                    cid = str(block.get("id") or "")
                    name = str(block.get("name") or "")
                    if cid:
                        names[cid] = name
                    # arguments 本来就是字符串，直接用
                    text = str(block.get("arguments") or "")
                    if text:
                        events.append(Event("tool_call", text, name=name, time=ts))
        elif typ == "tool/call":
            # 和 assistant 里的 tool-call 重复，不输出；只借它补全工具名
            data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
            cid = str(data.get("callId") or "")
            if cid:
                names[cid] = str(data.get("name") or "")
        elif typ == "tool/result":
            data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
            msg = data.get("message") if isinstance(data.get("message"), dict) else {}
            ts = _ms_iso(obj.get("time"))
            text = _content_text(msg.get("content"))
            if text:
                if msg.get("isError"):
                    text = "[错误] " + text
                events.append(Event(
                    "tool_result", text,
                    name=names.get(str(msg.get("toolCallId") or ""), ""),
                    time=ts,
                ))
        # 其他类型（system/message、developer/message、turn/*、step/*、request/*、session/*、permission/* …）跳过
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


def _ms_iso(ms) -> str:
    """毫秒时间戳 → 本地时间 ISO 字符串。"""
    if isinstance(ms, (int, float)):
        return datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds")
    return ""


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "…"
