from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

from kopitiam import backup, db, mcp_server, web
from kopitiam.acp import Host
from kopitiam.acp import managed
from kopitiam.cli import build_parser, main
from kopitiam.config import db_path
from kopitiam.wake import agy, service
from kopitiam.wake.detector import WakeCall
from test_acp import FAKE
from test_acp import permission


@pytest.fixture
def runtime(home):
    instance = managed.Runtime(host_factory=lambda backend: Host(command=FAKE, cwd=home))
    yield instance
    instance.close()


@pytest.fixture
def mcp_identity(monkeypatch, home):
    monkeypatch.setattr(mcp_server.atexit, "register", lambda fn: None)
    monkeypatch.setattr(db, "host_pid", os.getpid)
    monkeypatch.chdir(home)
    yield
    mcp_server._ME = None
    mcp_server._MANAGED = False
    mcp_server._BY_THREAD.clear()


def row(agent_id):
    with db._conn() as c:
        found = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return dict(found) if found else None


def test_terminal_start_has_no_identity_or_database_side_effect(mcp_identity):
    assert mcp_server.start("agy") is None
    assert not db_path().exists()
    mcp_server._release()
    assert not db_path().exists()
    assert "agy@" in mcp_server.whoami()
    agent = row(mcp_server._ME)
    assert agent["kind"] == "agy" and agent["host"] is None and agent["thread_id"] is None


def test_managed_mcp_claims_only_preallocated_row_and_does_not_release(home, mcp_identity):
    agent_id = db.create_acp(str(home))
    assert mcp_server.start("agy", agent_id=agent_id, host_pid=os.getpid()) == agent_id
    assert mcp_server._me() == agent_id
    assert row(agent_id)["pid"] == os.getpid()
    mcp_server._release()
    assert row(agent_id)["pid"] == os.getpid()
    assert "不能切换" in mcp_server.switch_identity()
    assert mcp_server.set_cwd(str(home)).startswith("错误：")


def test_terminal_metadata_uses_root_conversation_then_conversation_id(home, mcp_identity, monkeypatch):
    meta = {}
    monkeypatch.setattr(mcp_server.mcp, "get_context", lambda: SimpleNamespace(
        request_context=SimpleNamespace(meta=SimpleNamespace(model_extra=meta))))
    assert mcp_server.start("agy") is None
    meta.update({"antigravity.google/root_conversation_id": "root", "antigravity.google/conversation_id": "child-a"})
    first = mcp_server._me()
    meta["antigravity.google/conversation_id"] = "child-b"
    assert mcp_server._me() == first
    assert row(first)["thread_id"] == "root" and row(first)["host"] is None
    assert row(first)["cwd"] == str(home)
    meta.pop("antigravity.google/root_conversation_id")
    second = mcp_server._me()
    assert second != first and row(second)["thread_id"] == "child-b"
    meta.clear()
    fallback = mcp_server._me()
    assert fallback not in (first, second) and row(fallback)["thread_id"] is None


def test_managed_metadata_mismatch_logs_but_never_switches_identity(home, mcp_identity, monkeypatch, capsys):
    agent_id = db.create_acp(str(home))
    db.bind_acp(agent_id, "expected-id")
    mcp_server.start("agy", agent_id=agent_id, host_pid=os.getpid())
    monkeypatch.setattr(mcp_server, "_request_thread", lambda: ("other-id", None, None))
    assert mcp_server._me() == agent_id
    assert "conversation mismatch" in capsys.readouterr().err
    assert db.get_acp(agent_id)["session_id"] == "expected-id"


def test_queued_wake_checks_presence_again_before_sending_prompt(home, runtime, monkeypatch):
    locked = False
    monkeypatch.setattr(managed, "presence_locked", lambda sid: locked)
    record = runtime.create(str(home))
    host, sid = runtime._sessions[record["id"]]
    first = runtime.wake(record["id"], "permission")
    card = permission(host, sid)
    queued = runtime.wake(record["id"], "must not start")
    locked = True
    host.answer_permission(sid, card["request"], "once")
    assert first.result(5)["stopReason"] == "end_turn"
    with pytest.raises(ValueError, match="先在终端退出"):
        queued.result(5)
    assert "must not start" not in str(host.sessions[sid].activity.snapshot())


@pytest.mark.parametrize("kind,tag", [("claude", None), ("agy", None), ("agy", "wrong")])
def test_managed_mcp_rejects_other_identity_rows(home, mcp_identity, kind, tag):
    agent_id = db.claim(kind, cwd=str(home), pid=os.getpid())
    with db._conn() as c:
        c.execute("UPDATE agents SET host=? WHERE id=?", (tag, agent_id))
    with pytest.raises(ValueError):
        mcp_server.start("agy", agent_id=agent_id, host_pid=os.getpid())
    assert row(agent_id)["host"] == tag


def test_cli_reports_invalid_managed_arguments_on_stderr(home, capsys):
    assert build_parser().parse_args(["mcp", "--kind", "agy", "--agent-id", "1", "--host-pid", "2"]).agent_id == 1
    assert main(["mcp", "--kind", "agy", "--agent-id", "1"]) == 1
    captured = capsys.readouterr()
    assert "必须一起" in captured.err
    assert not captured.out


def test_terminal_and_thread_claims_never_take_managed_identity(home):
    managed_id = db.create_acp(str(home), name="agy@test", pid=99999999)
    terminal = db.claim("agy", cwd=str(home), pid=os.getpid())
    named = db.claim("agy", cwd=str(home), name="agy@test", pid=os.getpid())
    threaded = db.claim_thread("agy", "other-thread", str(home), pid=os.getpid())
    assert managed_id not in (terminal, named, threaded)
    assert row(managed_id)["host"] == "agy-acp"
    assert row(managed_id)["thread_id"] is None
    assert db.Chat().switch(terminal, "agy@test")[0] == terminal
    assert db.Chat().switch(managed_id, row(terminal)["name"])[0] == managed_id


def test_adopt_managed_sessions_excludes_handoff_archive_and_trash(home):
    ids = [db.create_acp(str(home), pid=99999999) for _ in range(4)]
    with db._conn() as c:
        c.execute("UPDATE agents SET last_seen='1970-01-01T00:00:00' WHERE kind='agy'")
        c.execute("UPDATE acp_sessions SET handed_off=? WHERE agent_id=?", (db.now_iso(), ids[1]))
        c.execute("UPDATE agents SET archived_at=? WHERE id=?", (db.now_iso(), ids[2]))
        c.execute("UPDATE agents SET trashed_at=? WHERE id=?", (db.now_iso(), ids[3]))
    assert db.adopt_acp() == [ids[0]]
    assert row(ids[0])["pid"] == os.getpid()
    assert all(row(i)["pid"] == 99999999 for i in ids[1:])


def test_managed_session_creation_and_web_restart(home, runtime):
    record = runtime.create(str(home), options={"model": "fake", "effort": "low"})
    assert record["thread_id"] == record["session_id"]
    assert record["host"] == "agy-acp"
    assert runtime.wake(record["id"], "first").result(5)["stopReason"] == "end_turn"
    runtime.close()
    second = managed.Runtime(host_factory=lambda backend: Host(command=FAKE, cwd=home))
    try:
        assert db.is_online(db.get_acp(record["id"]))
        assert second.wake(record["id"], "after restart").result(5)["stopReason"] == "end_turn"
    finally:
        second.close()


def test_runtime_permission_policy_reads_current_database_value(home, runtime):
    record = runtime.create(str(home), policy="all")
    host, sid = runtime._sessions[record["id"]]
    assert runtime.wake(record["id"], "permission").result(5)["stopReason"] == "end_turn"
    assert not host.snapshot()[sid]["permissions"]
    assert "'optionId': 'once'" in str(host.sessions[sid].activity.snapshot())
    with db._conn() as c:
        c.execute("UPDATE acp_sessions SET policy='workspace' WHERE agent_id=?", (record["id"],))
    pending = runtime.wake(record["id"], "permission")
    permission(host, sid)
    host.cancel(sid)
    assert pending.result(5)["stopReason"] == "cancelled"


def test_mcp_launch_parameters_bind_home_and_web_identity(home):
    definition = managed.mcp_server(42)
    assert definition["name"] == "kopitiam"
    assert definition["args"][-6:] == ["--kind", "agy", "--agent-id", "42", "--host-pid", str(os.getpid())]
    assert definition["env"] == [{"name": "KOPITIAM_HOME", "value": str(home.resolve())}]


def test_permanent_delete_closes_managed_session_before_removing_row(home, runtime):
    record = runtime.create(str(home))
    host, sid = runtime._sessions[record["id"]]
    assert db.Chat().trash(record["name"]).startswith("已")
    assert db.Chat().purge(record["name"]).startswith("已")
    assert row(record["id"]) is None
    assert db.get_acp(record["id"]) is None
    assert sid not in host.sessions


def test_agy_check_reasons_and_terminal_is_not_a_warning(home, monkeypatch):
    db.init_db()
    terminal = db.claim("agy", cwd=str(home), pid=os.getpid())
    assert agy.check(row(terminal)) == agy.TERMINAL
    assert not service.overview()["agents"][row(terminal)["name"]]["warn"]
    agent_id = db.create_acp(str(home))
    assert "开着 kopitiam web" in agy.check(row(agent_id))
    with db._conn() as c:
        c.execute("UPDATE acp_sessions SET handed_off=? WHERE agent_id=?", (db.now_iso(), agent_id))
    assert "已交给终端" in agy.check(row(agent_id))
    with db._conn() as c:
        c.execute("UPDATE acp_sessions SET handed_off=NULL WHERE agent_id=?", (agent_id,))
    runtime = managed.Runtime()
    try:
        monkeypatch.setattr(agy, "presence_locked", lambda sid: True)
        assert "先在终端退出" in agy.check(row(agent_id))
        monkeypatch.setattr(agy, "presence_locked", lambda sid: False)
        runtime.errors[agent_id] = "model unavailable"
        assert "model unavailable" in agy.check(row(agent_id))
        runtime.errors.clear()
        assert agy.check(row(agent_id)) is None
        called = []
        monkeypatch.setattr(runtime, "wake", lambda ident, text: called.append((ident, text)))
        agy.wake(WakeCall(agent_id, "agy", "agy", os.getpid(), [1], ["user"], text="read messages"))
        assert called == [(agent_id, "read messages")]
    finally:
        runtime.close()


@pytest.mark.parametrize("failure", ["auth", "crash"])
def test_failed_turn_is_hint_and_next_wake_recovers(runtime, home, failure):
    record = runtime.create(str(home))
    with pytest.raises(Exception):
        runtime.wake(record["id"], failure).result(5)
    assert agy.check(record) is None
    assert agy.hint(record)
    assert service.overview()["agents"][record["name"]]["hint"]
    assert runtime.wake(record["id"], "recover").result(5)["stopReason"] == "end_turn"
    assert agy.hint(record) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows presence byte lock")
def test_presence_checks_held_lock_not_file_existence(home):
    import msvcrt
    sid = str(uuid.uuid4())
    lock = home / "antigravity-cli" / "presence" / f"{sid}.lock"
    lock.parent.mkdir(parents=True)
    lock.write_bytes(b"0")
    assert not managed.presence_locked(sid, gemini_dir=home)
    with lock.open("r+b") as handle:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            assert managed.presence_locked(sid, gemini_dir=home)
        finally:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    assert not managed.presence_locked(sid, gemini_dir=home)
    assert not managed.presence_locked("../../unrelated", gemini_dir=home)


def test_restore_ignores_only_own_descendants(monkeypatch):
    child, other = 10000001, 10000002
    monkeypatch.setattr(backup.psutil, "Process", lambda: SimpleNamespace(children=lambda recursive: [SimpleNamespace(pid=child)]))
    monkeypatch.setattr(backup.psutil, "process_iter", lambda attrs: iter([
        SimpleNamespace(info={"pid": pid, "name": "kopitiam.exe", "cmdline": ["kopitiam", "mcp"]})
        for pid in [child, other]]))
    found = backup.other_processes()
    assert len(found) == 1 and str(other) in found[0]


def test_restore_pause_closes_acp_after_writes_drain(home):
    srv, _, _ = web.make_server(port=0, peer_port=0)
    closed = []
    srv.acp = SimpleNamespace(close=lambda: closed.append(srv.write_gate.closed))
    try:
        srv.pause_for_restore()
        assert closed == [True]
    finally:
        srv.server_close()
