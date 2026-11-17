from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from kopitiam import db
from kopitiam.export import agy, kind_for, export_context, ExportError

FIXTURE = Path(__file__).parent / "fixtures/agy.jsonl"


def test_parse_real_record_shapes_with_handwritten_fixture():
    events = agy.parse(FIXTURE)
    assert [e.kind for e in events] == ["user", "assistant", "tool_call", "tool_call",
                                        "tool_result", "tool_result", "assistant", "note", "note"]
    assert events[0].text == "检查商品卡片"
    assert events[0].time == "2026-11-17T08:00:00Z"
    assert [events[i].name for i in (2, 3)] == ["read_file", "mcp_kopitiam_whoami"]
    assert events[4].name == ""  # No reliable correlation ID in GENERIC rows.
    assert "文件内容" in events[4].text
    assert events[-2].text.startswith("kopitiam：")
    assert events[-1].text == "Authentication required"
    assert "元数据" not in str(events)
    assert kind_for(FIXTURE, "claude") == "agy"


def test_lookup_and_managed_export_use_exact_thread(home, monkeypatch):
    root = home / "brain"
    monkeypatch.setattr(agy, "ROOT", root)
    sid = str(uuid.uuid4())
    path = root / sid / ".system_generated/logs/transcript.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(FIXTURE.read_bytes())
    assert agy.find_by_thread(sid) == path
    assert agy.find_by_thread("../../other") is None
    assert agy.find_by_thread(str(uuid.uuid4())) is None
    agent = db.create_acp(str(home))
    db.bind_acp(agent, sid)
    result = db.Chat().export_context(agent)
    assert result.startswith("已导出")
    exported = next((home / "files").glob("export_*.md")).read_text(encoding="utf-8")
    assert "检查商品卡片" in exported and "read_file" in exported
    assert "隐藏元数据" not in exported


def test_terminal_requires_explicit_file_if_thread_is_missing(home, monkeypatch):
    monkeypatch.setattr(agy, "ROOT", home / "empty")
    with pytest.raises(ExportError, match="session_file"):
        export_context("agy", str(home), "agy@test")
    db.init_db()
    terminal = db.claim("agy", cwd=str(home))
    assert "session_file" in db.Chat().export_context(terminal)
    assert db.Chat().export_context(terminal, session_file=str(FIXTURE)).startswith("已导出")
