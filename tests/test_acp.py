from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil
import pytest

from kopitiam.acp import ACPError, Client, Host
from kopitiam.acp.activity import Activity

FAKE = [sys.executable, "-u", str(Path(__file__).with_name("fake_acp.py"))]


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError("Timed out waiting for fake ACP")


@pytest.fixture
def host(home):
    instance = Host(command=FAKE, cwd=home)
    yield instance
    instance.close()


def permission(host, sid):
    return until(lambda: host.snapshot()[sid]["permissions"])[0]


def test_rpc_correlates_out_of_order_responses_and_errors(home):
    client = Client(FAKE, cwd=home, log_file=home / "acp.log")
    try:
        assert client.start()["protocolVersion"] == 1
        with ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(client.request, "echo", {"delay": delay, "value": i}, timeout=3)
                       for i, delay in enumerate([.2, .01, .1, 0])]
            assert [future.result() for future in futures] == list(range(4))
        with pytest.raises(ACPError) as error:
            client.request("nonexistent", {}, timeout=3)
        assert error.value.code == -32601
        # Unknown incoming requests must not stop the reader or pending RPCs.
        client.request("test/unknown_request", {}, timeout=3)
        assert client.request("echo", {"value": "alive"}, timeout=3) == "alive"
    finally:
        client.close()


def test_wakes_queue_until_permission_and_turn_finish(host, home):
    session = host.new_session(home, [])
    first = host.wake(session.session_id, "slow")
    card = permission(host, session.session_id)
    second = host.wake(session.session_id, "second")
    assert host.snapshot()[session.session_id]["queued"] == 1
    assert not second.done()
    assert "second" not in str(session.activity.snapshot())
    host.answer_permission(session.session_id, card["request"], "once")
    assert first.result(5)["stopReason"] == "end_turn"
    assert second.result(5)["stopReason"] == "end_turn"
    assert session.state == "idle"
    assert "second:end" in str(session.activity.snapshot())


def test_malformed_frame_fails_waiters_and_stops_process(home):
    client = Client(FAKE, cwd=home, log_file=home / "acp.log")
    try:
        client.start()
        with pytest.raises(ACPError, match="stream failed"):
            client.request("test/bad_frame", {}, timeout=3)
        until(lambda: client.tree.process.poll() is not None)
    finally:
        client.close()


def test_permission_validation_and_cancel_do_not_block_reader(host, home):
    session = host.new_session(home, [])
    future = host.wake(session.session_id, "permission")
    card = permission(host, session.session_id)
    assert session.state == "waiting_permission"
    with pytest.raises(ValueError):
        host.answer_permission("other-session", card["request"], "once")
    with pytest.raises(ValueError):
        host.answer_permission(session.session_id, card["request"], "invented")
    assert host._client.request("echo", {"value": "reader available"}, timeout=2) == "reader available"
    host.cancel(session.session_id)
    assert future.result(5)["stopReason"] == "cancelled"
    until(lambda: not host.snapshot()[session.session_id]["permissions"])


def test_close_during_permission_cleans_up_waiters(host, home):
    session = host.new_session(home, [])
    future = host.wake(session.session_id, "permission")
    permission(host, session.session_id)
    process = psutil.Process(host._client.tree.process.pid)
    host.close()
    with pytest.raises(ACPError):
        future.result(5)
    assert not process.is_running()
    assert session.state == "not_loaded"
    until(lambda: not host._permissions)


def test_crash_sets_error_and_next_wake_restarts_and_resumes(host, home):
    session = host.new_session(home, [], options={"model": "fake", "mode": "default", "effort": "low"})
    generation = host._generation
    with pytest.raises(ACPError):
        host.wake(session.session_id, "crash").result(5)
    assert session.state == "error"
    assert host.wake(session.session_id, "recovered").result(5)["stopReason"] == "end_turn"
    assert host._generation == generation + 1
    assert session.state == "idle"


def test_job_cleanup_includes_descendants(host, home):
    session = host.new_session(home, [])
    host.wake(session.session_id, "child").result(5)
    event = next(e for e in session.activity.snapshot()["events"]
                 if e["update"].get("toolCallId") == "spawn")
    child = psutil.Process(event["update"]["rawInput"]["pid"])
    assert child.is_running()
    host.close()
    _, alive = psutil.wait_procs([child], timeout=3)
    assert not alive


def test_front_crash_cleans_child_that_inherits_stdout(host, home):
    session = host.new_session(home, [])
    started = time.monotonic()
    with pytest.raises(ACPError):
        host.wake(session.session_id, "orphan_crash").result(5)
    assert time.monotonic() - started < 5
    assert host.wake(session.session_id, "recovered").result(5)["stopReason"] == "end_turn"


def test_idle_process_reaps_only_after_all_sessions_unload(host, home):
    session = host.new_session(home, [])
    client = host._client
    host.reap_idle(client.last_used + 1801)
    assert client.running
    host.close_session(session.session_id)
    assert session.state == "not_loaded"
    host.reap_idle(client.last_used + 1799)
    assert client.running
    host.reap_idle(client.last_used + 1801)
    assert not client.running
    assert host.wake(session.session_id, "after idle").result(5)["stopReason"] == "end_turn"


def test_authentication_error_is_actionable(host, home):
    session = host.new_session(home, [])
    with pytest.raises(ACPError) as error:
        host.wake(session.session_id, "auth").result(5)
    assert error.value.code == -32000
    assert "agy acp --login" in session.error
    assert session.state == "error"


def test_activity_merges_chunks_and_preserves_cursor_updates():
    activity = Activity(turns=3, events=2)
    activity.new_turn()
    activity.append({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "one"}})
    before = activity.snapshot()
    activity.append({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "two"}})
    after = activity.snapshot(before["seq"])
    assert after["events"][0]["id"] == before["events"][0]["id"]
    assert after["events"][0]["update"]["content"]["text"] == "onetwo"
    for i in range(4):
        activity.new_turn()
        for j in range(4):
            activity.append({"sessionUpdate": "tool_call", "toolCallId": f"{i}:{j}"})
    assert len(activity.turns) == 3
    result = activity.snapshot(before["seq"])
    assert result["reset"]
    assert len(result["events"]) == 6
