"""导出测试：四个解析器、find_session、渲染、截短、遮盖、端到端导出。"""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

import pytest
from compression import zstd

from agent_chat.export import (
    ExportError,
    Event,
    export_context,
    kind_for,
    merge_adjacent,
    redact,
    render,
    truncate_events,
)
from agent_chat.export import claude as xclaude
from agent_chat.export import codex as xcodex
from agent_chat.export import dsh as xdsh
from agent_chat.export import pi as xpi

FIX = Path(__file__).parent / "fixtures"


def _kinds(events):
    return [e.kind for e in events]


def _header(agent="frontend"):
    return {
        "agent": agent,
        "kind": "claude",
        "cwd": "E:\\Proj",
        "source": "x.jsonl",
        "exported_at": datetime(2026, 10, 15, 14, 4, 55),
    }


# ---- claude ----


def test_parse_claude():
    events = xclaude.parse(FIX / "claude.jsonl")
    assert _kinds(events) == ["user", "assistant", "tool_call", "tool_result", "note", "note"]
    assert events[0].text == "帮我看下登录"
    assert events[1].text == "我先看代码"  # 空的 thinking 被跳过
    assert events[2].name == "Read"
    assert events[2].text == '{"file_path": "src/a.ts"}'
    assert events[3].text == "文件内容..."
    assert events[3].name == "Read"  # 工具名通过 tool_use_id 查到
    assert events[4].text.startswith("（上下文被压缩，以下为摘要）")
    assert "摘要内容" in events[4].text
    assert events[5].text == "命令：/clear"  # <command-name> 变成 note


# ---- codex ----


def test_parse_codex():
    events = xcodex.parse(FIX / "codex.jsonl")
    assert _kinds(events) == [
        "user", "thinking", "tool_call", "tool_result", "tool_call", "tool_result", "assistant", "note",
    ]
    assert events[0].text == "修一下脚步声"  # <environment_context> 被过滤，真实提问保留
    assert events[0].time != ""  # 尾部换行被 strip
    assert events[1].text == "想想"  # encrypted_content 忽略
    assert events[2].name == "exec"
    assert events[3].name == "exec"
    assert events[4].name == "request_user_input"
    assert events[5].name == "request_user_input"  # 工具名通过 call_id 查到
    assert events[5].text == '{"answers":{}}'
    assert events[6].text == "改好了"
    assert events[7].text.endswith("摘要文字...")


def test_parse_codex_event_msg_not_duplicated():
    events = xcodex.parse(FIX / "codex.jsonl")
    assert sum(1 for e in events if e.text == "修一下脚步声") == 1  # event_msg 不会造成重复


# ---- pi ----


def test_parse_pi_only_current_branch():
    events = xpi.parse(FIX / "pi.jsonl")
    assert _kinds(events) == [
        "user", "thinking", "assistant", "tool_call", "tool_result", "tool_result", "note",
    ]
    assert events[0].text == "研究一下这个"
    assert events[2].text == "好"
    assert "分支A" not in "".join(e.text for e in events)  # 只包含 m2b 那条分支
    assert events[3].name == "web_search"
    assert events[4].name == "web_search"
    assert events[5].name == "bash"
    assert events[5].text.startswith("$ git status")
    assert events[6].text.endswith("摘要...")


# ---- dsh ----


def _dsh_fixture(home: Path) -> Path:
    raw = (FIX / "dsh.jsonl").read_text(encoding="utf-8")
    p = home / "session-1.jsonl.zstd"
    p.write_bytes(zstd.compress(raw.encode("utf-8")))
    return p


def test_parse_dsh(home):
    events = xdsh.parse(_dsh_fixture(home))
    assert _kinds(events) == ["user", "note", "thinking", "assistant", "tool_call", "tool_result"]
    assert events[0].text == "看下地面材质"
    assert "注入的上下文" not in "".join(e.text for e in events)  # runtime-context 被跳过
    assert events[1].kind == "note" and "子代理消息" in events[1].text
    assert events[4].name == "glob"
    assert events[4].text == '{"pattern":"**/L_*"}'  # arguments 本来就是字符串
    assert events[5].text == "[错误] No files found"  # isError 加前缀
    assert events[5].name == "glob"
    expected = datetime.fromtimestamp(1792064189463 / 1000).strftime("%H:%M")
    assert expected in events[0].time  # 毫秒时间戳 → 本地 ISO


# ---- find_session ----


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_find_session_matches_cwd(home):
    _write(home / ".claude/projects/E--Proj/a.jsonl", '{"type":"user","cwd":"E:\\\\Proj"}\n')
    _write(home / ".claude/projects/D--Other/b.jsonl", '{"type":"user","cwd":"D:\\\\Other"}\n')
    got = xclaude.find_session("e:/proj", home=home)
    assert got is not None and got.name == "a.jsonl"


def test_find_session_ignores_case_and_slashes(home):
    _write(home / ".codex/sessions/2026/rollout-1.jsonl",
           '{"type":"session_meta","payload":{"cwd":"E:\\\\Proj"}}\n')
    got = xcodex.find_session("e:/proj/", home=home)
    assert got is not None and got.name == "rollout-1.jsonl"


def test_find_session_prefers_latest(home):
    a = home / ".pi/agent/sessions/p/old.jsonl"
    b = home / ".pi/agent/sessions/p/new.jsonl"
    _write(a, '{"type":"session","cwd":"Q:\\\\Work"}\n')
    _write(b, '{"type":"session","cwd":"Q:\\\\Work"}\n')
    old = time.time() - 500
    os.utime(a, (old, old))
    got = xpi.find_session("Q:\\Work", home=home)
    assert got is not None and got.name == "new.jsonl"  # 最新修改的那个


def test_find_session_dsh(home):
    raw = '{"type":"session","cwd":"E:\\\\Proj"}\n'
    p = home / ".dsh/sessions/a/b/session-1.jsonl.zstd"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(zstd.compress(raw.encode("utf-8")))
    got = xdsh.find_session("E:/Proj", home=home)
    assert got is not None and got.name == "session-1.jsonl.zstd"


def test_find_session_none(home):
    assert xclaude.find_session("E:\\Proj", home=home) is None


# ---- kind_for ----


def test_kind_for(home):
    z = home / "s.jsonl.zstd"
    z.write_bytes(zstd.compress(b'{"type":"session","cwd":"Q"}'))
    assert kind_for(z, "pi") == "dsh"
    meta = home / "r.jsonl"
    meta.write_text('{"type":"session_meta","payload":{"cwd":"E"}}', encoding="utf-8")
    assert kind_for(meta, "claude") == "codex"
    p = home / "p.jsonl"
    p.write_text('{"type":"session","cwd":"Q"}', encoding="utf-8")
    assert kind_for(p, "claude") == "pi"
    d = home / "d.jsonl"
    d.write_text('{"type":"session","createdAt":123,"cwd":"Q"}', encoding="utf-8")
    assert kind_for(d, "pi") == "dsh"
    c = home / "c.jsonl"
    c.write_text('{"type":"user","cwd":"E"}', encoding="utf-8")
    assert kind_for(c, "codex") == "claude"
    junk = home / "j.jsonl"
    junk.write_text("完全不是 JSON", encoding="utf-8")
    assert kind_for(junk, "codex") == "codex"  # 判断不出来就用默认


# ---- 合并 / 截短 ----


def test_merge_adjacent():
    merged = merge_adjacent([
        Event("user", "a"), Event("user", "b"),
        Event("assistant", "c"),
        Event("tool_call", "x", name="T"),
        Event("assistant", "d"),
    ])
    assert _kinds(merged) == ["user", "assistant", "tool_call", "assistant"]
    assert merged[0].text == "a\nb"


def test_truncate_clean():
    out = truncate_events([
        Event("user", "问题"),
        Event("thinking", "想" * 50),
        Event("tool_call", "c" * 400, name="Read"),
        Event("tool_result", "r" * 600, name="Read"),
    ], "clean")
    assert "thinking" not in _kinds(out)  # clean 模式 thinking 不出现
    assert "原文 400 字" in out[1].text
    assert "原文 600 字" in out[2].text
    assert out[0].text == "问题"  # user 不截短


def test_truncate_full_keeps_thinking():
    out = truncate_events([Event("thinking", "t" * 3000)], "full")
    assert "原文 3000 字" in out[0].text


# ---- 渲染 ----


def test_render_groups_assistant_run():
    events = [
        Event("user", "帮我把登录页改成用 OAuth", time="2026-10-15T06:01:00Z"),
        Event("assistant", "好的，我先看代码。", time="2026-10-15T06:01:05Z"),
        Event("tool_call", '{"file_path": "src/auth.ts"}', name="Read"),
        Event("tool_result", "import ...", name="Read"),
    ]
    md, turns = render(events, "clean", _header())
    assert md.startswith("# 上下文导出：frontend")
    assert "仅供参考" in md  # 防注入提示必须保留
    assert turns == 1
    assert md.count("## 👤 用户") == 1
    assert md.count("## 🤖 助手") == 1  # 连续 assistant/tool 放在同一个标题下
    assert "> 🔧 **Read** `{\"file_path\": \"src/auth.ts\"}`" in md
    assert "<details><summary>结果：Read</summary>" in md
    assert "…（截短" not in md  # clean 下不足 500 字不截短


def test_render_clean_drops_thinking_and_truncates():
    events = [
        Event("user", "q"),
        Event("thinking", "想" * 100),
        Event("tool_result", "r" * 800, name="Bash"),
    ]
    md, _ = render(events, "clean", _header())
    assert "💭" not in md
    assert "原文 800 字" in md


def test_render_fence_escapes_backticks():
    events = [Event("user", "q"), Event("tool_result", "```\ncode\n```", name="Read")]
    md, _ = render(events, "clean", _header())
    assert "````" in md  # 内容里有 ``` 时外层用 4 个反引号


def test_render_full_keeps_thinking():
    md, _ = render([Event("user", "q"), Event("thinking", "想一想")], "full", _header())
    assert "💭 想一想" in md


def test_render_total_length_drops_oldest():
    # clean 上限 2MB：200 条 × 15KB ≈ 3MB，必然触发删事件
    events = [Event("user", f"问题{i} " + "x" * 15000) for i in range(200)]
    md, turns = render(events, "clean", _header())
    assert "记录太长，省略了前面" in md
    assert "问题0" not in md  # 从最前面删
    assert "问题199" in md
    assert turns < 200


# ---- 遮盖 ----


@pytest.mark.parametrize("secret,marker", [
    ("sk-ant-" + "a" * 25, "sk-ant-"),
    ("sk-" + "b" * 25, "sk-"),
    ("ghp_" + "c" * 35, "ghp_"),
    ("github_pat_" + "d" * 35, "github_pat_"),
    ("AKIA" + "2" * 16, "AKIA"),
    ("AIza" + "e" * 35, "AIza"),
    ("xoxb-" + "f" * 12, "xoxb-"),
    ("eyJ" + "g" * 12 + "." + "h" * 12 + "." + "i" * 12, "eyJ"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----", "BEGIN RSA"),
])
def test_redact_simple_patterns(secret, marker):
    out = redact(secret)
    assert marker not in out or out == "[已遮盖]"
    assert "[已遮盖]" in out


def test_redact_private_key_block():
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    assert redact("前 " + block + " 后") == "前 [已遮盖] 后"


def test_redact_keeps_group():
    out = redact("Authorization: bearer " + "j" * 25)
    assert "bearer [已遮盖]" in out
    assert "j" * 25 not in out
    out = redact('api_key = "' + "k" * 20 + '"')
    assert 'api_key = "[已遮盖]"' in out
    assert "k" * 20 not in out
    out = redact("password: " + "m" * 12)
    assert "password: [已遮盖]" in out


# ---- 端到端 ----


def test_export_context_end_to_end(home, monkeypatch):
    root = home / "claude-root"
    f = root / "E--Proj" / "s.jsonl"
    _write(f, (FIX / "claude.jsonl").read_text(encoding="utf-8"))
    monkeypatch.setattr(xclaude, "ROOT", root)
    out, stats = export_context("claude", "E:\\Proj", "frontend")
    md = out.read_text(encoding="utf-8")
    assert md.startswith("# 上下文导出：frontend")
    assert "- 来源：" in md and "s.jsonl" in md
    assert stats["turns"] == 1  # 只有 1 个 user 事件（摘要/命令是 note，不是 user）
    assert stats["source"] == "s.jsonl"
    assert stats["bytes"] == len(md.encode("utf-8"))
    assert out.parent == home / "files"  # 落在 FILES_DIR
    assert out.name.startswith("export_2026") or out.name.startswith("export_")


def test_export_context_no_match_raises(home):
    with pytest.raises(ExportError) as ei:
        export_context("claude", "E:\\Proj", "frontend")
    assert "set_cwd" in str(ei.value)
    assert "session_file" in str(ei.value)


def test_export_context_bad_mode(home):
    with pytest.raises(ExportError):
        export_context("claude", "E:\\Proj", "frontend", mode="nope")


def test_export_context_session_file(home):
    out, stats = export_context("claude", "E:\\Proj", "frontend",
                                session_file=str(FIX / "claude.jsonl"))
    assert stats["source"] == "claude.jsonl"  # 跳过查找直接用指定文件
    assert "帮我看下登录" in out.read_text(encoding="utf-8")
