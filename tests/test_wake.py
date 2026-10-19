"""自动唤醒：感知模块的规则、Claude 适配器（信号文件 + hook）、安装 hook。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kopitiam import db
from kopitiam.config import wake_dir
from kopitiam.db import Chat
from kopitiam.wake import claude, detector, service


def _scan():
    """所有 agent 都能叫醒的假适配器：只看感知模块的判断。"""
    return detector.scan(lambda agent: None)


def _woken(calls) -> dict[str, list[int]]:
    return {c.name: c.message_ids for c in calls}


@pytest.fixture
def room(home):
    db.init_db()
    a = db.claim("claude", cwd=str(home), name="A", pid=os.getpid())
    b = db.claim("codex", cwd=str(home), name="B", pid=os.getpid())
    user = db.user_id()
    _scan()  # 第一次感知：记下起点
    return a, b, user


def _send(me: int, to: str | list[str], text: str = "hi") -> int:
    out = Chat().send(me, text, [to] if isinstance(to, str) else to)
    assert out.startswith("已发送 #"), out
    return int(out.split("#")[1].split()[0])


def _note(agent_id: int) -> str:
    return detector.states()[agent_id]["note"]


# ---- 感知模块 ----


def test_old_messages_are_not_woken(home):
    db.init_db()
    a = db.claim("claude", cwd=str(home), name="A", pid=os.getpid())
    _send(db.user_id(), "A")  # 感知开始之前的消息
    assert _scan() == []
    assert _scan() == []
    m = _send(db.user_id(), "A")
    assert _woken(_scan()) == {"A": [m]}
    assert detector.states()[a]["enabled"]


def test_user_message_wakes_once_until_read(room):
    a, b, user = room
    m1 = _send(user, "A", "第一条")
    calls = _scan()
    assert _woken(calls) == {"A": [m1]}
    assert "1 条新消息（来自 user）" in calls[0].text and "read" in calls[0].text
    m2 = _send(user, "A", "第二条")
    assert _scan() == []  # 上一批还没读：不重复叫
    Chat().read(a)
    m3 = _send(user, "A", "第三条")
    assert _woken(_scan()) == {"A": [m3]}  # 读过以后，新消息再叫；读掉的 m2 不算
    assert m2 < m3


def test_broadcast_and_own_messages_do_not_wake(room):
    a, b, user = room
    _send(user, [])  # 发给所有人
    _send(user, "all")
    _send(a, "A")  # 发给自己
    assert _scan() == []


def test_offline_agent_is_not_woken_and_gets_no_backlog(room, home):
    a, b, user = room
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        c = db.claim("pi", cwd=str(home), name="C", pid=proc.pid)
        _scan()
        proc.kill()
        proc.wait()
        _send(user, "C")
        assert _scan() == []
        db.claim("pi", cwd=str(home), name="C", pid=os.getpid())  # 重新上线
        assert _scan() == []  # 离线时的消息不补叫
        m = _send(user, "C")
        assert _woken(_scan()) == {"C": [m]}
    finally:
        proc.kill()


def test_pause_skips_and_resume_has_no_backlog(room):
    a, b, user = room
    detector.set_paused(True)
    _send(user, "A")
    assert _scan() == []
    detector.set_paused(False)
    assert _scan() == []
    m = _send(user, "A")
    assert _woken(_scan()) == {"A": [m]}


def test_disabled_agent_is_not_woken(room):
    a, b, user = room
    detector.set_enabled(a, False)
    _send(user, "A")
    assert _scan() == []
    detector.set_enabled(a, True)
    m = _send(user, "A")
    assert _woken(_scan()) == {"A": [m]}


def _ping_pong(a: int, b: int, n: int) -> list[tuple[str, list[int]]]:
    """A、B 轮流发 n 条，每条之后感知一次、收件人读掉；返回每次叫醒的情况。"""
    woken = []
    for i in range(n):
        me, to, to_id = (a, "B", b) if i % 2 == 0 else (b, "A", a)
        _send(me, to, f"第 {i + 1} 条")
        woken += [(c.name, c.message_ids) for c in _scan()]
        Chat().read(to_id)
    return woken


def test_agent_ping_pong_stops_after_max_rounds(room):
    a, b, user = room
    detector.set_max_rounds(2)  # 2 个来回 = 4 条
    woken = _ping_pong(a, b, 6)
    assert [w[0] for w in woken] == ["B", "A", "B", "A"]
    assert "连续来回超过 2 次" in _note(b)
    # 用户说一句话就重新计数
    _send(user, "B", "继续")
    assert [c.name for c in _scan()] == ["B"]
    Chat().read(b)
    assert [w[0] for w in _ping_pong(a, b, 2)] == ["B", "A"]


def test_default_max_rounds_is_six(room):
    a, b, user = room
    assert detector.max_rounds() == 6
    assert len(_ping_pong(a, b, 14)) == 12


def test_max_rounds_zero_only_user_wakes(room):
    a, b, user = room
    detector.set_max_rounds(0)
    _send(a, "B")
    assert _scan() == []
    m = _send(user, "B")
    assert _woken(_scan()) == {"B": [m]}
    with pytest.raises(ValueError):
        detector.set_max_rounds(-1)


def test_chain_resets_after_idle_gap(room):
    a, b, user = room
    detector.set_max_rounds(1)
    assert len(_ping_pong(a, b, 2)) == 2
    old = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    with closing(db.connect()) as c:
        c.execute("UPDATE messages SET created_at=?", (old,))  # 两个 agent 一小时没说话
    assert len(_ping_pong(a, b, 2)) == 2


def test_user_message_wakes_even_when_pair_is_over_limit(room):
    a, b, user = room
    detector.set_max_rounds(0)
    m_agent = _send(a, "B")
    m_user = _send(user, "B")
    assert _woken(_scan()) == {"B": [m_user]}  # 只叫 user 那条，agent 那条超过上限
    assert m_agent < m_user


def test_check_reason_is_recorded(room):
    a, b, user = room
    _send(user, "B")
    assert detector.scan(lambda agent: "装不上" if agent["name"] == "B" else None) == []
    assert _note(b) == "装不上"


# ---- service + Claude 适配器 ----


def test_claude_adapter_end_to_end(room, monkeypatch):
    a, b, user = room
    env = {"CLAUDE_PID": str(os.getpid())}
    start = json.loads(claude.hook_start(env))
    sig = Path(start["hookSpecificOutput"]["watchPaths"][0])
    assert sig == claude.signal_path(os.getpid()) and sig.exists() and sig.parent == wake_dir()

    m = _send(user, "A", "帮我看下")
    results = service.run_once()
    assert [(c.name, err) for c, err in results] == [("A", None)]
    assert "已叫醒" in _note(a)
    body = json.loads(sig.read_text(encoding="utf-8"))
    assert body["id"] == f"{a}-{m}"

    event = json.dumps({"file_path": str(sig), "event": "change"})
    code, text = claude.hook_wake(event, env)
    assert code == 2 and "kopitiam：你有 1 条新消息（来自 user）" in text
    assert claude.hook_wake(event, env) == (0, "")  # 同一次叫醒触发多次 FileChanged：只算一次

    # B 是 codex：还没在对话里用过 kopitiam（不知道是哪个对话），记下原因
    _send(user, "B")
    assert service.run_once() == []
    assert "还没在对话里用过 kopitiam" in _note(b)


def test_claude_hook_ignores_other_files_and_unlink(home):
    env = {"CLAUDE_PID": str(os.getpid())}
    claude.hook_start(env)
    sig = claude.signal_path(os.getpid())
    sig.write_text(json.dumps({"id": "x-1", "text": "hello"}), encoding="utf-8")
    other = home / "other.txt"
    other.write_text("x", encoding="utf-8")
    assert claude.hook_wake(json.dumps({"file_path": str(other), "event": "change"}), env) == (0, "")
    assert claude.hook_wake(json.dumps({"file_path": str(sig), "event": "unlink"}), env) == (0, "")
    assert claude.hook_wake("not json", env) == (0, "")
    assert claude.hook_wake(json.dumps({"file_path": str(sig), "event": "change"}), env) == (2, "hello")


def test_claude_check_and_failed_wake_can_retry(room):
    a, b, user = room
    _send(user, "A")
    assert service.run_once() == []  # 没有信号文件 = 没装 hook
    assert "kopitiam setup claude" in _note(a)

    claude.hook_start({"CLAUDE_PID": str(os.getpid())})
    m = _send(user, "A")
    claude.signal_path(os.getpid()).unlink()  # 感知以后、叫醒之前 Claude 关掉了
    calls = detector.scan(lambda agent: None)
    assert _woken(calls) == {"A": [m]}
    with pytest.raises(RuntimeError):
        claude.wake(calls[0])
    detector.record(calls[0], "信号文件不见了")
    assert "叫醒失败" in _note(a)
    claude.hook_start({"CLAUDE_PID": str(os.getpid())})
    m2 = _send(user, "A")
    assert [c.name for c, err in service.run_once()] == ["A"]  # 失败的那次不挡住下一次


def test_cleanup_removes_dead_signal_files(home):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    wake_dir().mkdir(parents=True)
    dead = claude.signal_path(proc.pid)
    dead.write_text("", encoding="utf-8")
    claude.hook_start({"CLAUDE_PID": str(os.getpid())})
    assert not dead.exists() and claude.signal_path(os.getpid()).exists()


# ---- 安装 hook ----


def test_install_keeps_other_settings_and_is_idempotent(home):
    settings = home / "claude" / "settings.json"
    settings.parent.mkdir()
    exe = r"C:\Users\me\.local\bin\kopitiam.exe"
    other = {"hooks": [{"type": "command", "command": "echo hi"}]}
    settings.write_text(json.dumps({"model": "opus", "hooks": {"SessionStart": [other]}}), encoding="utf-8")

    claude.install(settings, exe)
    claude.install(settings, exe)  # 再装一次不会重复
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["model"] == "opus"
    assert data["hooks"]["SessionStart"][0] == other and len(data["hooks"]["SessionStart"]) == 2
    wake = data["hooks"]["FileChanged"]
    assert len(wake) == 1 and wake[0]["hooks"][0]["asyncRewake"] is True
    assert wake[0]["hooks"][0]["args"] == ["hook", "claude-wake"]
    assert (settings.parent / "settings.json.bak").exists()

    claude.install(settings, exe, remove=True)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["hooks"] == {"SessionStart": [other]}


def test_install_creates_settings_file(home):
    settings = home / "new" / "settings.json"
    claude.install(settings, r"C:\x\kopitiam.exe")
    assert set(json.loads(settings.read_text(encoding="utf-8"))["hooks"]) == {"SessionStart", "FileChanged"}


# ---- 网页：总开关、来回上限、每个 agent 的开关 ----


def test_web_wake_controls(home):
    import threading
    import urllib.request

    from kopitiam import web

    srv, token, url = web.make_server(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, data=None):
        req = urllib.request.Request(base + path, headers={"X-Token": token, "Content-Type": "application/json"},
                                     data=None if data is None else json.dumps(data).encode("utf-8"),
                                     method="GET" if data is None else "POST")
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        a = db.claim("claude", cwd=str(home), name="A", pid=os.getpid())
        w = call("/api/wake")
        assert w["running"] is False  # 测试里的网页没跑自动唤醒
        assert w["paused"] is False and w["max_rounds"] == 6 == w["default_rounds"]
        assert "kopitiam setup claude" in w["agents"]["A"]["reason"]
        claude.hook_start({"CLAUDE_PID": str(os.getpid())})
        assert call("/api/state?after=0")["wake"]["agents"]["A"]["reason"] is None

        assert call("/api/wake/pause", {"paused": True})["paused"] is True and detector.paused()
        out = call("/api/wake/rounds", {"n": 3})
        assert out["ok"] and out["max_rounds"] == 3 and "3 个来回" in out["message"]
        assert call("/api/wake/rounds", {"n": "x"})["ok"] is False
        assert call("/api/wake/rounds", {"n": 101})["ok"] is False
        assert call("/api/wake/agent", {"name": "A", "enabled": False})["agents"]["A"]["enabled"] is False
        assert call("/api/wake/agent", {"name": "user", "enabled": False})["ok"] is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_service_heartbeat(home):
    db.init_db()
    s = service.WakeService(interval=0.05)
    s.start()
    try:
        import time
        for _ in range(100):
            if service.overview()["running"]:
                break
            time.sleep(0.05)
        assert service.overview()["running"]
    finally:
        s.stop()
    assert not service.overview()["running"]


# ---- pi 适配器 ----


def test_pi_adapter_wakes_through_signal_file(home):
    from kopitiam.wake import pi, signals

    db.init_db()
    p = db.claim("pi", cwd=str(home), name="P", pid=os.getpid())
    _scan()
    _send(db.user_id(), "P")
    assert service.run_once() == []
    assert "kopitiam setup pi" in _note(p)  # 扩展没装：没有信号文件

    sig = signals.path("pi", os.getpid())
    sig.parent.mkdir(parents=True, exist_ok=True)
    sig.write_text("", encoding="utf-8")  # pi 扩展启动时会建这个文件
    m = _send(db.user_id(), "P", "看一下")
    assert [(c.name, err) for c, err in service.run_once()] == [("P", None)]
    body = json.loads(sig.read_text(encoding="utf-8"))
    assert body["id"] == f"{p}-{m}" and "kopitiam：你有 1 条新消息" in body["text"]


def test_pi_install_and_remove(home):
    from kopitiam.wake import pi

    dest_dir = home / "pi-agent" / "extensions"
    dest = pi.install(dest_dir)
    assert dest == dest_dir / "kopitiam-wake.ts"
    assert dest.read_text(encoding="utf-8") == pi.EXTENSION.read_text(encoding="utf-8")
    assert "sendUserMessage" in dest.read_text(encoding="utf-8")
    pi.install(dest_dir, remove=True)
    assert not dest.exists()


def test_signals_cleanup_handles_every_kind(home):
    from kopitiam.wake import signals

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    wake_dir().mkdir(parents=True)
    dead = [signals.path(k, proc.pid) for k in ("claude", "pi")]
    alive = signals.path("pi", os.getpid())
    for f in dead + [alive]:
        f.write_text("", encoding="utf-8")
    signals.cleanup()
    assert not any(f.exists() for f in dead) and alive.exists()


# ---- Codex：记下对话 ID、Stop hook 接力、daemon 托管时用 codex queue ----


def test_mcp_records_codex_thread_id(home):
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session

    from kopitiam import mcp_server

    agent = mcp_server.start("codex", "C")

    async def go():
        async with create_connected_server_and_client_session(mcp_server.mcp) as client:
            await client.call_tool("whoami", {})  # 没带 _meta：不记
            assert _thread(agent) is None
            await client.call_tool("whoami", {}, meta={"threadId": "thread-1"})
            assert _thread(agent) == "thread-1"
            await client.call_tool("read", {}, meta={"threadId": "thread-2"})  # 换了对话
            assert _thread(agent) == "thread-2"

    anyio.run(go)


def _thread(agent_id: int):
    with closing(db.connect()) as c:
        return c.execute("SELECT thread_id FROM agents WHERE id=?", (agent_id,)).fetchone()[0]


@pytest.fixture
def codex_room(home, monkeypatch):
    from kopitiam.wake import codex

    db.init_db()
    c = db.claim("codex", cwd=str(home), name="codex", pid=os.getpid())
    db.set_thread(c, "thread-1")
    monkeypatch.setattr(codex, "_codex_pid", lambda: os.getpid())  # hook 由这个 codex 进程启动
    monkeypatch.setattr(codex, "hosted_by_daemon", lambda pid: False)  # 桌面版 / 命令行窗口内置服务
    _scan()
    return c, codex


def test_codex_check_needs_thread_and_hook(home, monkeypatch):
    from kopitiam.wake import codex

    monkeypatch.setattr(codex, "hosted_by_daemon", lambda pid: False)
    agent = {"kind": "codex", "pid": os.getpid(), "thread_id": None}
    assert "还没在对话里用过 kopitiam" in codex.check(agent)
    agent["thread_id"] = "t"
    assert "kopitiam setup codex" in codex.check(agent)
    monkeypatch.setattr(codex, "_codex_pid", lambda: os.getpid())
    codex.hook_start()
    assert codex.check(agent) is None and "闲着的时候叫不醒" in codex.hint(agent)
    monkeypatch.setattr(codex, "hosted_by_daemon", lambda pid: True)
    assert codex.hint(agent) is None


def test_codex_stop_hook_hands_off_to_the_right_thread(codex_room):
    c, codex = codex_room
    codex.hook_start()
    _send(db.user_id(), "codex", "帮我 review")
    assert [(x.name, err) for x, err in service.run_once()] == [("codex", None)]
    assert _note(c).startswith("已交给 Codex")

    assert codex.hook_stop(json.dumps({"session_id": "other-thread"})) == ""  # 别的对话结束：不交
    out = json.loads(codex.hook_stop(json.dumps({"session_id": "thread-1", "stop_hook_active": False})))
    assert out["decision"] == "block" and "kopitiam：你有 1 条新消息" in out["reason"]
    assert codex.hook_stop(json.dumps({"session_id": "thread-1"})) == ""  # 同一次只交一次


def test_codex_stop_hook_skips_when_already_read(codex_room):
    c, codex = codex_room
    codex.hook_start()
    _send(db.user_id(), "codex")
    service.run_once()
    Chat().read(c)  # 用户自己先让它读了
    assert codex.hook_stop(json.dumps({"session_id": "thread-1"})) == ""


def test_codex_daemon_uses_codex_queue(codex_room, monkeypatch):
    c, codex = codex_room
    monkeypatch.setattr(codex, "hosted_by_daemon", lambda pid: True)
    monkeypatch.setattr(codex, "_exe", lambda pid: r"C:\daemon\codex.exe")
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "Queued message", ""

    monkeypatch.setattr(codex.subprocess, "run", lambda args, **kw: calls.append(args) or Done())
    _send(db.user_id(), "codex")
    assert [(x.name, err) for x, err in service.run_once()] == [("codex", None)]
    assert calls[0][:4] == [r"C:\daemon\codex.exe", "queue", "--thread", "thread-1"]
    assert "kopitiam：你有 1 条新消息" in calls[0][5]
    assert _note(c).startswith("已叫醒")


def test_codex_install_keeps_other_hooks(home):
    from kopitiam.wake import codex

    path = home / "codex" / "hooks.json"
    path.parent.mkdir()
    other = {"hooks": [{"type": "command", "command": "edge.exe", "timeout": 10}]}
    path.write_text(json.dumps({"description": "mine", "hooks": {"Stop": [other]}}), encoding="utf-8")
    exe = r"C:\Users\me\.local\bin\kopitiam.exe"
    codex.install(path, exe)
    codex.install(path, exe)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["description"] == "mine"
    assert data["hooks"]["Stop"][0] == other and len(data["hooks"]["Stop"]) == 2
    assert data["hooks"]["Stop"][1]["hooks"][0]["command"] == exe + " hook codex-stop"
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == exe + " hook codex-start"
    busy = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert busy["command"] == exe + " hook codex-busy" and busy["async"] is True  # 不拖慢你发消息
    codex.install(path, exe, remove=True)
    assert json.loads(path.read_text(encoding="utf-8"))["hooks"] == {"Stop": [other]}
    assert codex.hook_entries(r"C:\Program Files\k\kopitiam.exe")["Stop"]["hooks"][0]["command"].startswith('"')


def test_codex_export_uses_thread_file(home, monkeypatch):
    from kopitiam.export import codex as codex_export

    sessions = home / "fake-codex" / "sessions" / "2026" / "10" / "19"
    sessions.mkdir(parents=True)
    proj = home / "proj"
    proj.mkdir()
    rollout = sessions / "rollout-2026-10-19T10-00-00-abc-123.jsonl"
    rollout.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"type": "session_meta", "payload": {"id": "abc-123", "cwd": str(proj)}},
        {"type": "response_item", "timestamp": "2026-10-19T10:00:01Z",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "你好"}]}},
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(codex_export, "ROOT", home / "fake-codex" / "sessions")
    assert codex_export.find_by_thread("abc-123") == rollout
    assert codex_export.find_by_thread("nope") is None and codex_export.find_by_thread("../x") is None

    db.init_db()
    c = db.claim("codex", cwd=r"C:\Codex\bin", name="codex", pid=os.getpid())  # 桌面版的工作目录不对
    db.set_thread(c, "abc-123")
    out = Chat().export_context(c)
    assert out.startswith("已导出") and rollout.name in out


# ---- Codex 闲着时：弹 Windows 通知提醒用户 ----


def test_toast_xml_escapes_and_links():
    from kopitiam.wake import notify

    xml = notify.toast_xml("a <b> & c", "正文", "codex://threads/x?a=1&b=2")
    assert 'activationType="protocol"' in xml and "a &lt;b&gt; &amp; c" in xml
    assert 'launch="codex://threads/x?a=1&amp;b=2"' in xml
    assert "<toast>" in notify.toast_xml("t", "b")  # 没有链接：点了不打开东西


def _old(path: Path, seconds: int) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def _hand_off(codex) -> Path:
    codex.hook_start()
    _send(db.user_id(), "codex", "帮我 review")
    service.run_once()
    return signals_path("codex")


def test_codex_remind_fast_when_hooks_say_idle(codex_room, toasts):
    c, codex = codex_room
    codex.hook_busy(json.dumps({"session_id": "thread-1"}))  # 开始一轮
    sig = _hand_off(codex)
    _old(sig, 60)
    codex.remind()
    assert toasts == []  # 正在一轮里：做完 Stop hook 会接力，不打扰

    codex.hook_stop(json.dumps({"session_id": "other-thread"}))  # 别的对话结束不算
    codex.remind()
    assert toasts == []

    codex.hook_busy(json.dumps({"session_id": "thread-1"}))
    codex._set_busy("thread-1", False)  # 这一轮结束、没有可接力的 → 闲着
    _old(sig, codex.REMIND_IDLE - 2)
    codex.remind()
    assert toasts == []  # 刚交出去，给它两三秒
    _old(sig, codex.REMIND_IDLE + 1)
    codex.remind()
    codex.remind()
    assert len(toasts) == 1  # 同一批只提醒一次
    title, body, link = toasts[0]
    assert "codex 有新消息在等它" in title and "user 发来 1 条" in body
    assert link == "codex://threads/thread-1"


def test_codex_stop_records_idle_and_busy(codex_room):
    c, codex = codex_room
    codex.hook_start()
    codex.hook_stop(json.dumps({"session_id": "thread-1"}))  # 没东西可接力：这一轮结束
    assert codex._busy("thread-1") is False
    codex.hook_busy(json.dumps({"session_id": "thread-1"}))
    assert codex._busy("thread-1") is True
    _send(db.user_id(), "codex")
    service.run_once()
    assert codex.hook_stop(json.dumps({"session_id": "thread-1"}))  # 接力：接着忙
    assert codex._busy("thread-1") is True
    codex._set_busy("thread-1", True)
    st = codex._state_path("thread-1")
    st.write_text(json.dumps({"busy": True, "at": time.time() - codex.STATE_STALE - 1}), encoding="utf-8")
    assert codex._busy("thread-1") is None  # 「正在忙」记得太久（Codex 中途崩了）：不信它


def test_codex_remind_falls_back_to_rollout_without_state(codex_room, toasts, home, monkeypatch):
    from kopitiam.export import codex as codex_export

    c, codex = codex_room
    sig = _hand_off(codex)  # 没装 codex-busy hook：不知道忙不忙
    rollout = home / "rollout-fake.jsonl"  # 对话记录：最近还在动 = 在忙
    rollout.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(codex_export, "find_by_thread", lambda t: rollout)
    _old(sig, codex.REMIND_AFTER + 1)
    codex.remind()
    assert toasts == []
    _old(rollout, codex.IDLE_FOR + 1)  # 记录文件好一阵没动：当它闲着
    codex.remind()
    assert len(toasts) == 1


def test_codex_remind_skips_taken_signal(codex_room, toasts):
    c, codex = codex_room
    codex.hook_start()
    _send(db.user_id(), "codex")
    service.run_once()
    codex.hook_stop(json.dumps({"session_id": "thread-1"}))  # 已经被 Stop hook 接走
    _old(signals_path("codex"), 120)
    codex.remind()
    assert toasts == []


def signals_path(kind: str) -> Path:
    from kopitiam.wake import signals

    return signals.path(kind, os.getpid())


# ---- dsh：cordis 插件 + cordis.patch.yml ----


def test_dsh_install_appends_marked_block_and_removes_it(home):
    from kopitiam.wake import dsh

    profile = home / "profiles" / "web"
    profile.mkdir(parents=True)
    original = "# 我的 patch\n- id: llm\n  name: x\n- insert:\n    - id: kopitiam\n      config:\n        serverName: kopitiam\n"
    (profile / "cordis.patch.yml").write_text(original, encoding="utf-8")
    (home / "profiles" / "desktop").mkdir()
    (home / "profiles" / "desktop" / "cordis.patch.yml").write_text("- id: other\n", encoding="utf-8")
    assert dsh.target_profiles(home / "profiles") == [profile]  # 只装进配了 kopitiam MCP 的

    plugin = dsh.install(profile)
    dsh.install(profile)  # 再装一次不会重复
    text = (profile / "cordis.patch.yml").read_text(encoding="utf-8")
    assert text.startswith(original) and text.count(dsh.BEGIN) == 1 and text.count("id: kopitiam-wake") == 1
    assert f"name: '{plugin}'" in text
    assert plugin.read_text(encoding="utf-8") == dsh.PLUGIN.read_text(encoding="utf-8")
    assert (profile / "cordis.patch.yml.bak-kopitiam").exists()

    dsh.install(profile, remove=True)
    assert (profile / "cordis.patch.yml").read_text(encoding="utf-8").rstrip("\n") == original.rstrip("\n")
    assert not plugin.exists()
    assert "''" in dsh.patch_block(Path("C:/it's/kopitiam-wake.mjs"))  # YAML 单引号要写两遍


def test_dsh_adapter_uses_signal_file(home):
    db.init_db()
    d = db.claim("dsh", cwd=str(home), name="dsh", pid=os.getpid())
    _scan()
    _send(db.user_id(), "dsh")
    assert service.run_once() == []
    assert "kopitiam setup dsh" in _note(d)
    sig = signals_path("dsh")
    sig.parent.mkdir(parents=True, exist_ok=True)
    sig.write_text("", encoding="utf-8")  # 插件知道会话以后会建这个文件
    m = _send(db.user_id(), "dsh", "看一下")
    assert [(x.name, err) for x, err in service.run_once()] == [("dsh", None)]
    assert json.loads(sig.read_text(encoding="utf-8"))["id"] == f"{d}-{m}"


NODE_DRIVER = r"""
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
const { apply, inject, name } = await import(pathToFileURL(process.argv[2]).href);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const handlers = {}, disposers = [], prompts = [], warns = [];
const ctx = {
  on: (event, fn) => { handlers[event] = fn; },
  effect: (fn) => { disposers.push(fn()); },
  logger: { warn: (m) => warns.push(m) },
  sessionController: { prompt: async (req) => { prompts.push(req); return { accepted: true }; } },
};
apply(ctx);
const dir = join(process.env.KOPITIAM_HOME, "wake");
const signal = join(dir, `dsh-${process.pid}.signal`);
const before = existsSync(signal);
const passed = await handlers["tools/post-execute"](
  { name: "mcp__kopitiam__whoami", agent: { session: { header: { id: "s-1" } } } }, {}, async () => "next");
await handlers["tools/post-execute"]({ name: "read_file", agent: { session: { header: { id: "s-2" } } } }, {}, async () => "next");
const after = existsSync(signal);
writeFileSync(signal, JSON.stringify({ id: "5-9", text: "kopitiam：你有 1 条新消息" }));
await sleep(500);
writeFileSync(signal, JSON.stringify({ id: "5-9", text: "kopitiam：你有 1 条新消息" }));  // 同一次再触发
await sleep(500);
disposers.forEach((d) => d());
console.log(JSON.stringify({ name, inject, before, after, passed, prompts, warns,
  remembered: readFileSync(join(dir, "dsh-session.txt"), "utf8"), removed: !existsSync(signal) }));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="需要 Node.js")
def test_dsh_plugin_prompts_the_kopitiam_session(home):
    from kopitiam.wake import dsh

    driver = home / "driver.mjs"
    driver.write_text(NODE_DRIVER, encoding="utf-8")
    r = subprocess.run(["node", str(driver), str(dsh.PLUGIN)], capture_output=True, text=True,
                       encoding="utf-8", timeout=60, env=os.environ | {"KOPITIAM_HOME": str(home)})
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["name"] == "kopitiam-wake" and out["inject"] == ["sessionController"]
    assert out["before"] is False and out["after"] is True  # 知道会话以后才算能叫醒
    assert out["passed"] == "next"  # 不挡别的插件
    assert out["remembered"] == "s-1"  # 只记调用 kopitiam 工具的会话
    assert out["prompts"] == [{"requestId": "kopitiam-5-9", "sessionId": "s-1", "mode": "queue",
                               "content": [{"type": "text", "text": "kopitiam：你有 1 条新消息"}]}]
    assert out["warns"] == [] and out["removed"] is True
