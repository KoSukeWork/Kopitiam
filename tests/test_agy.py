"""agy（Antigravity CLI）：终端里只接 MCP；有人点名发消息时只弹通知提醒用户，绝不驱动 agy。"""
from __future__ import annotations

import os
from contextlib import closing
from types import SimpleNamespace

import pytest

from kopitiam import db, mcp_server
from kopitiam.cli import build_parser
from kopitiam.config import wake_dir
from kopitiam.wake import agy, detector, service


@pytest.fixture
def mcp_identity(monkeypatch, home):
    monkeypatch.setattr(mcp_server.atexit, "register", lambda fn: None)
    monkeypatch.setattr(db, "host_pid", os.getpid)
    monkeypatch.chdir(home)
    yield
    mcp_server._ME = None
    mcp_server._BY_THREAD.clear()


def _row(agent_id):
    with closing(db.connect()) as c:
        found = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return dict(found) if found else None


def test_cli_accepts_agy_kind():
    assert build_parser().parse_args(["mcp", "--kind", "agy"]).kind == "agy"
    assert build_parser().parse_args(["export", "--agent", "agy", "--cwd", "."]).agent == "agy"


def test_start_claims_nothing_until_first_tool_call(mcp_identity, home):
    assert mcp_server.start("agy") is None
    db.init_db()
    with closing(db.connect()) as c:
        assert not c.execute("SELECT 1 FROM agents WHERE kind='agy'").fetchone()
    assert "agy@" in mcp_server.whoami()  # 没带对话 ID：按目录认领
    agent = _row(mcp_server._ME)
    assert agent["kind"] == "agy" and agent["host"] is None and agent["thread_id"] is None


def test_metadata_uses_root_conversation_then_conversation_id(home, mcp_identity, monkeypatch):
    meta = {}
    monkeypatch.setattr(mcp_server.mcp, "get_context", lambda: SimpleNamespace(
        request_context=SimpleNamespace(meta=SimpleNamespace(model_extra=meta))))
    assert mcp_server.start("agy") is None
    meta.update({"antigravity.google/root_conversation_id": "root", "antigravity.google/conversation_id": "child-a"})
    first = mcp_server._me()
    meta["antigravity.google/conversation_id"] = "child-b"  # 子 agent：归到主对话
    assert mcp_server._me() == first
    assert _row(first)["thread_id"] == "root" and _row(first)["host"] is None and _row(first)["cwd"] == str(home)
    meta.pop("antigravity.google/root_conversation_id")
    second = mcp_server._me()
    assert second != first and _row(second)["thread_id"] == "child-b"


@pytest.fixture
def room(home):
    """一个开着的 agy、一个别的 agent；感知模块从现在开始算。"""
    db.init_db()
    target = db.claim_thread("agy", "conv-1", str(home / "shop-web"), pid=os.getpid())
    other = db.claim("claude", cwd=str(home), name="claude@api", pid=os.getpid())
    detector.scan(lambda agent: None)
    return target, other


def test_message_to_agy_only_pops_a_notification(room, toasts, monkeypatch):
    target, other = room
    touched = []
    monkeypatch.setattr(agy, "signals", SimpleNamespace(take=agy.signals.take,
                                                        write=lambda *a, **k: touched.append("signal")))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: touched.append("process"))
    db.Chat().send(other, "接口改好了，你接着做前端", to=[_row(target)["name"]])
    results = service.run_once()
    assert [(call.name, err) for call, err in results] == [(_row(target)["name"], None)]
    title, body = toasts[0]
    assert "有新消息" in title and "claude@api" in body and "shop-web" in body and "看一下聊天室" in body
    assert touched == []  # 不写信号文件、不启动任何进程：绝不驱动 agy
    assert "已弹通知提醒你" in service.overview()["agents"][_row(target)["name"]]["note"]


def test_notify_once_per_batch_and_clear_after_read(room, toasts, monkeypatch):
    target, other = room
    db.Chat().send(other, "第一条", to=[_row(target)["name"]])
    (call, _), = service.run_once()
    assert len(toasts) == 1
    assert agy.wake(call) == "已经弹过通知，等你在 agy 窗口里让它读"  # 同一批再交一次（比如 10 分钟后重试）
    assert len(toasts) == 1
    agy.remind()
    assert ("removed", f"agy-{target}") not in toasts  # 还没读，通知留着
    db.Chat().read(target)
    agy.remind()
    assert ("removed", f"agy-{target}") in toasts
    assert not list((wake_dir() / "agy-toast").glob("*.json"))


def test_user_messages_do_not_notify(room, toasts):
    target, _ = room
    db.Chat().send(db.user_id(), "你看一下这个", to=[_row(target)["name"]])
    (call, err), = service.run_once()
    assert err is None and toasts == []


def test_overview_explains_and_is_not_a_warning(room):
    target, _ = room
    info = service.overview()["agents"][_row(target)["name"]]
    assert info["supported"] and info["reason"] is None and not info["warn"] and info["notify_only"]
    assert "Google" in info["hint"] and "看一下聊天室" in info["hint"]
    assert not service.overview()["agents"]["claude@api"]["notify_only"]


def test_sidebar_badge_is_a_grey_bell_for_notify_only():
    import json
    import shutil
    import subprocess
    from kopitiam import web
    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed to exercise the inline badge function")
    fn = "function wakeBadge(" + web.INDEX_HTML.split("function wakeBadge(", 1)[1].split("\n}\n", 1)[0] + "\n}"
    stubs = """
const made = [];
function icon(name) { const c = new Set(); const b = {name, title: "", classList: {add: (...x) => x.forEach(v => c.add(v)), has: v => c.has(v)}, c}; made.push(b); return b; }
let wakeInfo = {running: true, paused: false, agents: {
  "agy@x": {enabled: true, reason: null, notify_only: true, hint: "只提醒", note: ""},
  "claude@x": {enabled: true, reason: null, notify_only: false, hint: null, note: ""}}};
const out = {};
for (const name of ["agy@x", "claude@x"]) { const b = wakeBadge(name); out[name] = {icon: b.name, classes: [...b.c], title: b.title}; }
console.log(JSON.stringify(out));
"""
    script = stubs.replace("const out", fn + "\nconst out")
    r = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
    assert r["agy@x"]["icon"] == "bell" and "idle" in r["agy@x"]["classes"] and "会被自动唤醒" not in r["agy@x"]["title"]
    assert r["claude@x"]["icon"] == "bolt" and "会被自动唤醒" in r["claude@x"]["title"]
