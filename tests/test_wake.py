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
    dead += [wake_dir() / f"dsh-{proc.pid}.calling.json", wake_dir() / f"dsh-{proc.pid}.wake-1-2.json",
             wake_dir() / "dsh-session.txt"]  # 最后一个是以前的 dsh 插件留下的
    alive = [signals.path("pi", os.getpid()), wake_dir() / f"dsh-{os.getpid()}.wake-1-3.json"]
    for f in dead + alive:
        f.write_text("", encoding="utf-8")
    signals.cleanup()
    assert not any(f.exists() for f in dead) and all(f.exists() for f in alive)


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


def test_mcp_codex_one_identity_per_thread(home, monkeypatch):
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session

    from kopitiam import mcp_server
    from kopitiam.export import codex as codex_export
    from kopitiam.wake import codex

    fake = home / "fake-codex"
    day = fake / "sessions" / "2026" / "10" / "19"
    day.mkdir(parents=True)
    proj = home / "Client"
    for tid in ("t-1", "t-2"):  # 两个对话都在 Client 文件夹（Codex 自己的工作目录不一定是它）
        (day / f"rollout-2026-10-19T10-00-00-{tid}.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"id": tid, "cwd": str(proj)}}) + "\n", encoding="utf-8")
    (fake / "session_index.jsonl").write_text(
        json.dumps({"id": "t-2", "thread_name": "查材质标签"}, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr(codex_export, "ROOT", fake / "sessions")
    monkeypatch.setattr(codex, "hosted_by_desktop", lambda pid: True)

    assert mcp_server.start("codex") is None  # 启动时还不知道是哪个对话：不认领

    async def go():
        async with create_connected_server_and_client_session(mcp_server.mcp) as client:
            out = [await client.call_tool("whoami", {}, meta={"threadId": t}) for t in ("t-1", "t-2", "t-1")]
            out.append(await client.call_tool("who", {}, meta={"threadId": "t-2"}))
            return [r.content[0].text for r in out]

    me1, me2, again, who = anyio.run(go)
    assert me1.startswith("你是 codex@Client（") and str(proj) in me1
    assert me2.startswith("你是 codex@Client-2（") and again == me1
    assert "codex@Client-2" in who and "（对话：查材质标签）" in who
    with closing(db.connect()) as c:
        rows = {r["name"]: r for r in c.execute("SELECT * FROM agents WHERE kind='codex'")}
    assert set(rows) == {"codex@Client", "codex@Client-2"}
    assert rows["codex@Client"]["thread_id"] == "t-1" and rows["codex@Client-2"]["host"] == "codex-desktop"

    mcp_server._release()  # 桌面版卸载了这个对话：桌面版还开着就还叫得醒，不标离线
    with closing(db.connect()) as c:
        assert all(db.is_online(r) for r in c.execute("SELECT * FROM agents WHERE kind='codex'"))
        c.execute("UPDATE agents SET pid=NULL WHERE kind='codex'")  # 桌面版关了
    mcp_server.start("codex")  # 重开：以前的对话身份先接上，不用等它们再调用一次
    with closing(db.connect()) as c:
        assert all(db.is_online(r) for r in c.execute("SELECT * FROM agents WHERE kind='codex'"))


def test_mcp_codex_on_daemon_goes_offline_with_its_conversation(home, monkeypatch):
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session

    from kopitiam import mcp_server
    from kopitiam.wake import codex

    monkeypatch.setattr(codex, "hosted_by_desktop", lambda pid: False)
    monkeypatch.setattr(codex, "hosted_by_daemon", lambda pid: True)
    window = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # 用户开着的命令行窗口
    monkeypatch.setattr(codex, "cli_window", lambda cwds: window.pid)
    mcp_server.start("codex")

    def row():
        with closing(db.connect()) as c:
            return c.execute("SELECT * FROM agents WHERE thread_id='t-1'").fetchone()

    async def whoami():
        async with create_connected_server_and_client_session(mcp_server.mcp) as client:
            await client.call_tool("whoami", {}, meta={"threadId": "t-1"})

    try:
        anyio.run(whoami)
        r = row()
        assert r["pid"] == window.pid and r["host"] == "codex-daemon" and db.is_online(r)
    finally:
        window.kill()
        window.wait(10)
    # daemon 关了窗口还会把对话留很久（这个 MCP 进程还在）：看窗口，关了就离线
    assert not db.is_online(row())
    mcp_server.start("codex")  # daemon 重开、重新加载这个对话：没有窗口，不能接上
    assert not db.is_online(row())

    again = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # 重开窗口，接着用这个对话
    monkeypatch.setattr(codex, "cli_window", lambda cwds: again.pid)
    try:
        anyio.run(whoami)
        assert row()["pid"] == again.pid and db.is_online(row())
    finally:
        again.kill()
        again.wait(10)


def test_cli_window_is_the_codex_tui_in_that_folder(monkeypatch):
    import psutil

    from kopitiam.wake import codex

    class P:
        def __init__(self, pid, cmd, cwd, t):
            self.pid, self._cmd, self._cwd = pid, cmd, cwd
            self.info = {"name": "codex.exe", "create_time": t}

        def cmdline(self):
            return self._cmd

        def cwd(self):
            return self._cwd

    procs = [P(1, ["codex.exe", "app-server", "--listen", "unix://"], r"C:\proj", 9),  # daemon
             P(2, ["codex.exe", "queue", "--thread", "t"], r"C:\proj", 8),  # 我们自己跑的 codex queue
             P(3, ["codex.exe"], r"C:\proj", 1),
             P(4, ["codex.exe", "resume"], r"c:/PROJ", 2),  # 同一个文件夹、更新的窗口
             P(5, ["codex.exe"], r"C:\other", 3)]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
    assert codex.cli_window([r"C:\proj"]) == 4
    assert codex.cli_window([r"C:\nothing"]) is None


def test_codex_daemon_found_above_the_mcp_process(monkeypatch):
    import psutil

    from kopitiam.wake import codex

    class P:
        def __init__(self, exe, parents=()):
            self._exe, self._parents = exe, list(parents)

        def exe(self):
            return self._exe

        def parents(self):
            return self._parents

    daemon = r"C:\Users\u\.codex\packages\app-server-daemon\releases\0.163.0\bin\codex.exe"
    tree = {7: P(r"C:\uv\python.exe", [P(r"C:\bin\kopitiam.exe"), P(daemon)]),
            8: P(r"C:\uv\python.exe", [P(r"C:\Codex\bin\codex.exe")])}
    monkeypatch.setattr(psutil, "Process", lambda pid: tree[pid])
    assert codex._daemon_exe(7) == daemon and codex.hosted_by_daemon(7)
    assert codex._daemon_exe(8) is None and not codex.hosted_by_daemon(8)


def test_thread_titles_follow_renames(home):
    from kopitiam.export import codex as codex_export

    index = home / ".codex" / "session_index.jsonl"
    index.parent.mkdir()
    index.write_text(json.dumps({"id": "t", "thread_name": "旧标题"}, ensure_ascii=False) + "\n", encoding="utf-8")
    assert codex_export.thread_title("t", home) == "旧标题"
    with index.open("a", encoding="utf-8") as f:  # 改名会追加一行
        f.write(json.dumps({"id": "t", "thread_name": "新标题"}, ensure_ascii=False) + "\n")
    assert codex_export.thread_title("t", home) == "新标题"
    assert codex_export.thread_title("x", home) is None


def _thread(agent_id: int):
    with closing(db.connect()) as c:
        return c.execute("SELECT thread_id FROM agents WHERE id=?", (agent_id,)).fetchone()[0]


@pytest.fixture
def codex_room(home, monkeypatch):
    from kopitiam.export import codex as codex_export
    from kopitiam.wake import codex

    monkeypatch.setattr(codex_export, "ROOT", home / "fake-codex" / "sessions")  # 不读真的 ~/.codex
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
    monkeypatch.setattr(codex, "hosted_by_daemon", lambda pid: False)  # pid 是用户开着的命令行窗口
    with closing(db.connect()) as conn:
        conn.execute("UPDATE agents SET host='codex-daemon' WHERE id=?", (c,))
    monkeypatch.setattr(codex, "_queue_exe", lambda pid: r"C:\cli\codex.exe")
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "Queued message", ""

    monkeypatch.setattr(codex.subprocess, "run", lambda args, **kw: calls.append(args) or Done())
    _send(db.user_id(), "codex")
    assert [(x.name, err) for x, err in service.run_once()] == [("codex", None)]
    assert calls[0][:4] == [r"C:\cli\codex.exe", "queue", "--thread", "thread-1"]
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
    assert "点这里打开它的对话" in body  # 不知道标题
    assert link == "codex://threads/thread-1"


def test_codex_remind_names_thread_and_clears_when_taken(codex_room, toasts, home):
    c, codex = codex_room
    index = home / "fake-codex" / "session_index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"id": "thread-1", "thread_name": "旧名字"},
        {"id": "thread-2", "thread_name": "别的对话"},
        {"id": "thread-1", "thread_name": "查找脚步特效"},  # 改过名：取最后一条
    ]) + "\n", encoding="utf-8")
    codex._set_busy("thread-1", False)
    sig = _hand_off(codex)
    _old(sig, codex.REMIND_IDLE + 1)
    codex.remind()
    assert len(toasts) == 1 and "点这里打开「查找脚步特效」" in toasts[0][1]

    codex.hook_stop(json.dumps({"session_id": "thread-1"}))  # 用户说了一句，这一轮结束时接走了
    codex.remind()
    codex.remind()
    assert toasts[1:] == [("removed", f"codex-{c}")]  # 通知收掉，只收一次


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


# ---- Codex 桌面版：排进 Codex 的共享队列 ----

# 和 Codex 的 queue_1.sqlite 一样（迁移 1、2 + 记录改动的触发器）
QUEUE_SCHEMA = """
CREATE TABLE _sqlx_migrations (version BIGINT PRIMARY KEY, description TEXT NOT NULL,
    installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, success BOOLEAN NOT NULL,
    checksum BLOB NOT NULL, execution_time BIGINT NOT NULL);
INSERT INTO _sqlx_migrations (version, description, success, checksum, execution_time)
    VALUES (1, 'queued items', 1, x'00', 0), (2, 'queued thread revisions', 1, x'00', 0);
CREATE TABLE queued_items (id TEXT PRIMARY KEY NOT NULL, thread_id TEXT NOT NULL, payload_json TEXT NOT NULL,
    queue_order INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL);
CREATE UNIQUE INDEX queued_items_thread_order_idx ON queued_items(thread_id, queue_order);
CREATE TABLE queued_thread_revisions (revision INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL UNIQUE);
CREATE TRIGGER queued_items_revision_after_insert AFTER INSERT ON queued_items BEGIN
    INSERT INTO queued_thread_revisions (thread_id) VALUES (NEW.thread_id) ON CONFLICT(thread_id) DO UPDATE
    SET revision = (SELECT COALESCE(MAX(revision), 0) + 1 FROM queued_thread_revisions);
END;
"""


@pytest.fixture
def codex_queue(home, monkeypatch):
    import sqlite3

    ch = home / "codex-home"
    ch.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(ch))
    with closing(sqlite3.connect(ch / "queue_1.sqlite")) as c:
        c.executescript(QUEUE_SCHEMA)
    return ch / "queue_1.sqlite"


def _queue_rows(path: Path) -> list[tuple]:
    import sqlite3

    with closing(sqlite3.connect(path)) as c:
        return c.execute("SELECT id, thread_id, payload_json, queue_order FROM queued_items"
                         " ORDER BY thread_id, queue_order").fetchall()


def test_enqueue_writes_codex_format_once(codex_queue):
    import sqlite3
    import uuid

    from kopitiam.wake import codex

    codex.enqueue("t1", "你好", "9-20")
    codex.enqueue("t1", "你好", "9-20")  # 同一次叫醒只排一次
    codex.enqueue("t1", "再来", "9-21")
    rows = _queue_rows(codex_queue)
    assert [r[3] for r in rows] == [0, 1]  # 排在最后
    assert json.loads(rows[0][2]) == {"UserInput": {
        "content": [{"type": "text", "text": "你好", "text_elements": []}], "client_id": codex._client_id("9-20")}}
    assert uuid.UUID(rows[0][0]).version == 7  # 和 Codex 自己的编号一样按时间排序
    with closing(sqlite3.connect(codex_queue)) as c:  # 触发器记下改动，Codex 靠它发现队列变了
        assert c.execute("SELECT COUNT(*) FROM queued_thread_revisions WHERE thread_id='t1'").fetchone()[0] == 1
        assert codex.queued("t1", "9-20")
        c.execute("DELETE FROM queued_items WHERE queue_order = 0")
        c.commit()
    assert not codex.queued("t1", "9-20") and codex.queued("t1", "9-21")


def test_queue_refuses_unknown_format(codex_queue, home, monkeypatch):
    import sqlite3

    from kopitiam.wake import codex

    assert codex.queue_problem() is None
    with closing(sqlite3.connect(codex_queue)) as c:
        c.execute("INSERT INTO _sqlx_migrations (version, description, success, checksum, execution_time)"
                  " VALUES (3, 'new', 1, x'00', 0)")
        c.commit()
    assert "格式变了" in codex.queue_problem()
    with pytest.raises(codex.QueueError):
        codex.enqueue("t1", "你好", "9-20")
    assert _queue_rows(codex_queue) == []
    monkeypatch.setenv("CODEX_HOME", str(home / "nowhere"))
    assert "没找到" in codex.queue_problem()
    assert not (home / "nowhere").exists()  # 不会替 Codex 建文件


def test_desktop_wake_queues_then_links_then_reminds(codex_room, codex_queue, toasts, monkeypatch):
    import sqlite3

    c, codex = codex_room
    monkeypatch.setattr(codex, "hosted_by_desktop", lambda pid: True)
    links: list[str] = []
    monkeypatch.setattr(codex, "_open_link", links.append)
    agent = {"kind": "codex", "pid": os.getpid(), "thread_id": "thread-1"}
    assert codex.check(agent) is None and codex.hint(agent) is None  # 不用装 hook，闲着也叫得醒

    _send(db.user_id(), "codex", "帮我 review")
    [(call, err)] = service.run_once()
    assert err is None and detector.states()[c]["note"].startswith("已叫醒")
    [(_, thread, payload, _)] = _queue_rows(codex_queue)
    assert thread == "thread-1" and "kopitiam：你有 1 条新消息" in payload
    assert not signals_path("codex").exists()  # 没走 Stop hook 接力

    pending = codex._pending_path(c)

    def age(seconds):
        obj = json.loads(pending.read_text(encoding="utf-8"))
        obj["at"] = time.time() - seconds
        pending.write_text(json.dumps(obj), encoding="utf-8")

    codex.remind()
    assert links == []  # 对话加载着的话桌面版几秒内自己就拿走了
    codex._set_busy("thread-1", True)
    age(codex.WAKE_LINK_AFTER + 1)
    codex.remind()
    assert links == []  # 正在一轮里：这一轮做完自己会拿
    codex._set_busy("thread-1", False)
    codex.remind()
    codex.remind()
    assert links == ["codex://threads/wake/thread-1"]  # 多半没加载：叫桌面版加载，只叫一次
    assert toasts == []
    age(codex.REMIND_QUEUED + 1)
    codex.remind()
    codex.remind()
    assert len(toasts) == 1 and "已经排进它的队列" in toasts[0][1] and toasts[0][2] == "codex://threads/thread-1"

    with closing(sqlite3.connect(codex_queue)) as q:  # Codex 拿走了
        q.execute("DELETE FROM queued_items")
        q.commit()
    codex.remind()
    assert not pending.exists() and toasts[-1] == ("removed", f"codex-{c}")


def test_desktop_falls_back_to_handoff_when_queue_changed(codex_room, codex_queue, monkeypatch):
    import sqlite3

    c, codex = codex_room
    monkeypatch.setattr(codex, "hosted_by_desktop", lambda pid: True)
    with closing(sqlite3.connect(codex_queue)) as q:
        q.execute("ALTER TABLE queued_items ADD COLUMN extra TEXT")
        q.commit()
    agent = {"kind": "codex", "pid": os.getpid(), "thread_id": "thread-1"}
    assert "kopitiam setup codex" in codex.check(agent)  # 队列用不了，又没装 hook
    assert "格式变了" in codex.hint(agent)
    codex.hook_start()
    assert codex.check(agent) is None
    _send(db.user_id(), "codex")
    service.run_once()
    assert detector.states()[c]["note"].startswith(codex.HANDED_OFF)
    assert "kopitiam：你有 1 条新消息" in signals_path("codex").read_text(encoding="utf-8")
    assert _queue_rows(codex_queue) == []


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

    old = profile / "kopitiam-wake.mjs"
    old.write_text("// 以前装的版本", encoding="utf-8")
    plugin = dsh.install(profile)
    dsh.install(profile)  # 再装一次不会重复
    assert plugin.name.startswith("kopitiam-wake-") and not old.exists()  # 文件名带哈希，旧版本删掉
    text = (profile / "cordis.patch.yml").read_text(encoding="utf-8")
    assert text.startswith(original) and text.count(dsh.BEGIN) == 1 and text.count("id: kopitiam-wake") == 1
    assert f"name: '{plugin}'" in text
    assert plugin.read_text(encoding="utf-8") == dsh.PLUGIN.read_text(encoding="utf-8")
    assert (profile / "cordis.patch.yml.bak-kopitiam").exists()

    dsh.install(profile, remove=True)
    assert (profile / "cordis.patch.yml").read_text(encoding="utf-8").rstrip("\n") == original.rstrip("\n")
    assert not plugin.exists()
    assert "''" in dsh.patch_block(Path("C:/it's/kopitiam-wake.mjs"))  # YAML 单引号要写两遍


def test_setup_dsh_says_restart_when_the_plugin_changed(home, monkeypatch, capsys):
    from kopitiam import cli
    from kopitiam.wake import dsh

    profile = home / "profiles" / "web"
    profile.mkdir(parents=True)
    (profile / "cordis.patch.yml").write_text("- insert:\n    - config:\n        serverName: kopitiam\n",
                                              encoding="utf-8")
    monkeypatch.setattr(dsh, "profiles_dir", lambda: home / "profiles")
    assert cli.main(["setup", "dsh"]) == 0
    assert "web profile 会自动重新加载" in capsys.readouterr().out  # 第一次装
    assert cli.main(["setup", "dsh"]) == 0
    assert "web profile 会自动重新加载" in capsys.readouterr().out  # 同一个版本再装一次
    (profile / dsh.installed_name()).rename(profile / "kopitiam-wake-00000000.mjs")
    assert cli.main(["setup", "dsh"]) == 0
    assert "要重开 dsh 才生效" in capsys.readouterr().out  # 换掉了以前装的版本


def test_dsh_adapter_wakes_the_session_of_that_identity(home):
    from kopitiam.wake import dsh

    db.init_db()
    d = db.claim_thread("dsh", "session-1", str(home / "Proj"), pid=os.getpid(), host="dsh")
    shared = db.claim("dsh", cwd=str(home), name="dsh", pid=os.getpid())  # 以前共用的身份，还不知道会话
    _scan()
    _send(db.user_id(), "dsh@Proj")
    assert service.run_once() == []
    assert "kopitiam setup dsh" in _note(d)  # 插件没装（插件一加载就建这个文件）
    sig = signals_path("dsh")
    sig.parent.mkdir(parents=True, exist_ok=True)
    sig.write_text("", encoding="utf-8")
    m = _send(db.user_id(), "dsh@Proj", "看一下")
    _send(db.user_id(), "dsh", "你呢")
    assert [(x.name, err) for x, err in service.run_once()] == [("dsh@Proj", None)]
    assert "还不知道该发给哪个会话" in _note(shared)
    wake = sig.parent / f"dsh-{os.getpid()}.wake-{d}-{m}.json"  # 每次叫醒一个文件，写明发给哪个会话
    assert json.loads(wake.read_text(encoding="utf-8")) == {
        "id": f"{d}-{m}", "text": wake_text_for(["user"], 1), "session": "session-1"}
    assert not list(sig.parent.glob("*.tmp"))


def wake_text_for(senders, n):
    return detector.wake_text(senders, n)


def test_mcp_dsh_one_identity_per_session(home, monkeypatch):
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session

    from kopitiam import mcp_server

    proj = home / "Client"
    calling = wake_dir() / f"dsh-{db.host_pid()}.calling.json"
    calling.parent.mkdir(parents=True, exist_ok=True)
    assert mcp_server.start("dsh") is None  # 启动时还不知道是哪个会话

    async def whoami(session, title=""):
        # dsh 的插件在调用 kopitiam 之前写下是哪个会话
        calling.write_text(json.dumps({"session": session, "cwd": str(proj), "title": title}), encoding="utf-8")
        async with create_connected_server_and_client_session(mcp_server.mcp) as client:
            r = await client.call_tool("whoami", {})
        calling.unlink()
        return r.content[0].text

    assert anyio.run(whoami, "session-1").startswith("你是 dsh@Client（")
    assert anyio.run(whoami, "session-2", "查材质").startswith("你是 dsh@Client-2（")
    assert anyio.run(whoami, "session-1", "改名以后").startswith("你是 dsh@Client（")
    who = Chat().who()
    assert "（对话：查材质）" in who and "（对话：改名以后）" in who  # 标题跟着会话
    with closing(db.connect()) as c:
        rows = {r["thread_id"]: r for r in c.execute("SELECT * FROM agents WHERE kind='dsh'")}
    assert set(rows) == {"session-1", "session-2"} and rows["session-1"]["host"] == "dsh"


def test_mcp_switch_identity_swaps_conversations(home):
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session

    from kopitiam import mcp_server

    proj = home / "Client"
    calling = wake_dir() / f"dsh-{db.host_pid()}.calling.json"
    calling.parent.mkdir(parents=True, exist_ok=True)
    mcp_server.start("dsh")

    async def call(session, tool, args=None, title=""):
        calling.write_text(json.dumps({"session": session, "cwd": str(proj), "title": title}), encoding="utf-8")
        async with create_connected_server_and_client_session(mcp_server.mcp) as client:
            r = await client.call_tool(tool, args or {})
        calling.unlink()
        return r.content[0].text

    assert anyio.run(call, "session-1", "whoami", None, "旧任务").startswith("你是 dsh@Client（")
    assert anyio.run(call, "session-2", "whoami", None, "新任务").startswith("你是 dsh@Client-2（")
    Chat().send(db.user_id(), "给旧任务的", ["dsh@Client"])
    assert "dsh@Client  在线" in anyio.run(call, "session-2", "switch_identity")  # dsh 开着：同一个 agent，能换

    # 新会话接手旧会话的身份（名字、未读）；两个会话交换身份
    out = anyio.run(call, "session-2", "switch_identity", {"name": "dsh@Client"}, "新任务")
    assert out.startswith("已换成 dsh@Client（原来的 dsh@Client-2 还在") and "未读消息 1 条" in out
    assert anyio.run(call, "session-2", "whoami", None, "新任务").startswith("你是 dsh@Client（")
    assert anyio.run(call, "session-1", "whoami", None, "旧任务").startswith("你是 dsh@Client-2（")
    with closing(db.connect()) as c:
        rows = {r["name"]: r for r in c.execute("SELECT * FROM agents WHERE kind='dsh'")}
    assert rows["dsh@Client"]["thread_id"] == "session-2" and rows["dsh@Client"]["title"] == "新任务"
    assert rows["dsh@Client-2"]["thread_id"] == "session-1" and rows["dsh@Client-2"]["title"] == "旧任务"


NODE_DRIVER = r"""
import { existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
const { apply, inject, name } = await import(pathToFileURL(process.argv[2]).href);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const handlers = {}, disposers = [], prompts = [], warns = [], seen = [];
const ctx = {
  on: (event, fn) => { handlers[event] = fn; },
  effect: (fn) => { disposers.push(fn()); },
  get: (key) => key === "sessionTitle" ? { get: (s) => ({ title: "标题-" + s.header.id }) } : undefined,
  logger: { warn: (m) => warns.push(m) },
  // 和 dsh 真的 SessionController.prompt 一样：第二个参数 signal 必填
  sessionController: { prompt: async (req, signal) => { signal.throwIfAborted(); prompts.push(req); return { accepted: true }; } },
};
const dir = join(process.env.KOPITIAM_HOME, "wake");
const prefix = `dsh-${process.pid}`;
const calling = join(dir, `${prefix}.calling.json`);
apply(ctx);
const ready = existsSync(join(dir, `${prefix}.signal`));
const call = (tool, id) => handlers["tools/execute"](
  { name: tool, agent: { session: { header: { id, cwd: "C:\\proj" } } } },
  async () => {
    seen.push(existsSync(calling) ? JSON.parse(readFileSync(calling, "utf8")) : null);
    await sleep(50);  // 调用期间别的会话来调用：要排队，不能把 calling 文件改掉
    seen.push(existsSync(calling) ? JSON.parse(readFileSync(calling, "utf8")).session : null);
    return "next-" + id;
  });
const results = await Promise.all([call("mcp__kopitiam__read", "s-1"), call("mcp__kopitiam__send", "s-2")]);
const other = await call("read_file", "s-3");
const left = existsSync(calling);
const wake = join(dir, `${prefix}.wake-5-9.json`);
writeFileSync(wake + ".tmp", JSON.stringify({ id: "5-9", text: "kopitiam：你有 1 条新消息", session: "s-2" }));
renameSync(wake + ".tmp", wake);
await sleep(500);
const consumed = !existsSync(wake);
writeFileSync(join(dir, "dsh-1.wake-7-1.json"), JSON.stringify({ id: "7-1", text: "别的 dsh 的", session: "x" }));
await sleep(300);
disposers.forEach((d) => d());
console.log(JSON.stringify({ name, inject, ready, results, other, seen, left, consumed, prompts, warns,
  removed: !existsSync(join(dir, `${prefix}.signal`)) }));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="需要 Node.js")
def test_dsh_plugin_tells_kopitiam_the_session_and_wakes_it(home):
    from kopitiam.wake import dsh

    driver = home / "driver.mjs"
    driver.write_text(NODE_DRIVER, encoding="utf-8")
    r = subprocess.run(["node", str(driver), str(dsh.PLUGIN)], capture_output=True, text=True,
                       encoding="utf-8", timeout=60, env=os.environ | {"KOPITIAM_HOME": str(home)})
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["name"] == "kopitiam-wake" and out["inject"] == ["sessionController"]
    assert out["ready"] is True  # 一加载就算装好了
    assert out["results"] == ["next-s-1", "next-s-2"] and out["other"] == "next-s-3"  # 不挡调用
    # 两个会话同时调用：排队，各自调用期间 calling 文件写的都是自己
    assert out["seen"][:4] == [{"session": "s-1", "cwd": "C:\\proj", "title": "标题-s-1"}, "s-1",
                               {"session": "s-2", "cwd": "C:\\proj", "title": "标题-s-2"}, "s-2"]
    assert out["seen"][4:] == [None, None]  # 别的工具不写
    assert out["left"] is False  # 调用完就删
    assert out["consumed"] is True and out["prompts"] == [
        {"requestId": "kopitiam-5-9", "sessionId": "s-2", "mode": "queue",
         "content": [{"type": "text", "text": "kopitiam：你有 1 条新消息"}]}]  # 别的 dsh 进程的不管
    assert out["warns"] == [] and out["removed"] is True


# ---- 叫醒了但没送到：agent 重开过 / 太久没读，就再交一次 ----


def _woke(calls):
    return [(c.name, c.message_ids, c.wake_id) for c in calls]


def test_rewake_after_agent_restarts(home):
    old, new = (subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) for _ in range(2))
    try:
        db.init_db()
        c = db.claim("dsh", cwd=str(home), name="C", pid=old.pid)
        _scan()
        m = _send(db.user_id(), "C")
        first = _woke(_scan())
        assert first == [("C", [m], f"{c}-{m}")]
        assert _scan() == []  # 同一个进程、还没读：不重复叫
        old.kill()
        old.wait()
        assert db.claim("dsh", cwd=str(home), name="C", pid=new.pid) == c  # C 重开了（旧的那次多半没送到）
        assert _woke(_scan()) == first  # 没有新消息也再交一次；叫醒编号一样，接收端会去重
        assert _scan() == []  # 交给新进程以后就不再重复
    finally:
        old.kill()
        new.kill()


def test_retry_when_unread_for_too_long(room):
    from kopitiam.config import WAKE_RETRY

    a, b, user = room
    m = _send(user, "A")
    calls = _scan()
    detector.record(calls[0], None)
    assert _scan() == []
    with closing(db.connect()) as c:
        c.execute("UPDATE wake_state SET woken_at=? WHERE agent_id=?", (time.time() - WAKE_RETRY - 1, a))
    m2 = _send(user, "A")
    assert _woke(_scan()) == [("A", [m, m2], f"{a}-{m2}")]  # 从没读的地方重新交（含新来的）
    Chat().read(a)
    m3 = _send(user, "A")
    assert _woke(_scan()) == [("A", [m3], f"{a}-{m3}")]  # 读过以后照常


# ---- 手动归档 ----


def test_archived_agent_is_hidden_and_not_woken_until_it_comes_back(home):
    db.init_db()
    me = db.claim("claude", cwd=str(home), name="me", pid=os.getpid())
    c = db.claim_thread("codex", "t-1", str(home / "Client"), pid=os.getpid(), host="codex-desktop")
    _scan()
    assert Chat().archive("codex@Client") == "已归档 codex@Client"
    assert "codex@Client" not in Chat().who()  # 不在 who 里列出
    out = Chat().send(me, "在吗", ["codex@Client"])
    assert "已归档：消息会保存，但不会叫醒它" in out  # 照常保存，告诉发送者一声
    assert _woken(_scan()) == {}  # 不叫醒

    db.release(c)  # 桌面版关了又重开：归档的不接上
    assert db.adopt("codex", "codex-desktop", os.getpid()) == 0

    # 那个对话自己又用 kopitiam 了：自动回到列表，之后的消息照常叫醒
    assert db.claim_thread("codex", "t-1", str(home / "Client"), pid=os.getpid(), host="codex-desktop") == c
    assert "codex@Client" in Chat().who()
    _send(me, "codex@Client", "现在呢")
    assert list(_woken(_scan())) == ["codex@Client"]

    Chat().archive("codex@Client")
    Chat().whoami(c)  # 调用任何工具都算在用
    assert "codex@Client" in Chat().who()
    assert Chat().archive("user").startswith("错误：")


def test_trash_deletes_agent_with_its_messages_after_30_days(home):
    from kopitiam.config import files_dir

    db.init_db()
    user = db.user_id()
    old = db.claim("codex", cwd=str(home), name="old", pid=os.getpid())
    b = db.claim("claude", cwd=str(home), name="b", pid=os.getpid())
    src = home / "plan.md"
    src.write_text("计划", encoding="utf-8")
    own = _send(old, "user", "它发的")
    Chat().send(old, "带附件", ["user"], [str(src)])
    with_file = max(int(r["id"]) for r in Chat().messages_after(0))
    shared = Chat().messages_after(0)[-1]["files"][0]["path"]
    Chat().forward(user, with_file, ["b"])  # 转发和原消息共用同一个附件文件
    Chat().send(old, "另一个附件", ["user"], [str(src)])
    alone_file = Chat().messages_after(0)[-1]["files"][0]["path"]
    only = _send(user, "old", "只发给它")
    both = _send(user, ["old", "b"], "发给两个")
    _send(user, "all", "广播")

    assert Chat().trash("old").startswith("已移到回收站：old")
    assert "old" not in Chat().who()
    assert "old 在回收站：消息会保存，但不会叫醒它" in Chat().send(user, "还在吗", ["old"])
    assert db.purge_stale() == 0  # 还没到 30 天

    with closing(db.connect()) as c:
        c.execute("UPDATE agents SET trashed_at='2020-01-01T00:00:00' WHERE id=?", (old,))
    assert db.purge_stale() == 1
    with closing(db.connect()) as c:
        names = {r[0] for r in c.execute("SELECT name FROM agents")}
        left = {r["id"]: r for r in c.execute("SELECT * FROM messages")}
    assert "old" not in names
    assert own not in left and only not in left and with_file not in left  # 它发的、只发给它的删掉
    assert json.loads(left[both]["to_ids"]) == [b]  # 同时发给别人的留下，收件人去掉它
    assert any(m["text"] == "广播" for m in left.values())
    assert Path(shared).exists()  # 转发的消息还在用
    assert not Path(alone_file).exists() and Path(alone_file).parent == files_dir()


def test_trash_restore_purge_now_and_restore_on_use(home):
    db.init_db()
    a = db.claim("codex", cwd=str(home), name="a", pid=os.getpid())
    _send(a, "user", "hi")
    assert Chat().purge("a").startswith("错误：")  # 不在回收站里不能永久删除
    Chat().trash("a")
    assert Chat().trash("a", False) == "已从回收站恢复 a"
    Chat().trash("a")
    Chat().whoami(a)  # 它自己又用 kopitiam 了
    assert "a" in Chat().who()
    Chat().trash("a")
    assert Chat().purge("a") == "已永久删除 a"
    with closing(db.connect()) as c:
        assert c.execute("SELECT COUNT(*) FROM agents WHERE name='a'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_mcp_reclaims_after_its_identity_was_deleted(home, monkeypatch):
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session

    from kopitiam import mcp_server
    from kopitiam.wake import codex

    monkeypatch.setattr(codex, "hosted_by_desktop", lambda pid: True)
    mcp_server.start("codex")

    async def whoami():
        async with create_connected_server_and_client_session(mcp_server.mcp) as client:
            r = await client.call_tool("whoami", {}, meta={"threadId": "t-1"})
            return r.content[0].text

    first = anyio.run(whoami)
    name = first.split("你是 ")[1].split("（")[0]
    Chat().trash(name)
    Chat().purge(name)  # 回收站里永久删了，可这个对话还开着
    again = anyio.run(whoami)
    assert again.startswith(f"你是 {name}（")  # 重新认领了一个身份，不报错
