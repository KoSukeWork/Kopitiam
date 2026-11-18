from __future__ import annotations

import json
import threading

import pytest

from kopitiam import db, web
from kopitiam.acp import Host, managed
from kopitiam.wake import service
from test_acp import FAKE, permission, until
from test_web import _base, _get, _post


@pytest.fixture
def server(home):
    srv, token, url = web.make_server(port=0, peer_port=0)
    srv.acp = managed.Runtime(host_factory=lambda backend: Host(command=FAKE, cwd=home))
    srv.acp.web_url = url
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, token
    srv.shutdown()
    srv.server_close()
    thread.join()


def post(server, path, data):
    srv, token = server
    status, raw = _post(_base(srv) + path, json.dumps(data).encode(), {"X-Token": token})
    return status, json.loads(raw)


def get(server, path):
    srv, token = server
    status, raw = _get(_base(srv) + path, {"X-Token": token})
    return status, json.loads(raw)


@pytest.mark.parametrize("method,path", [("GET", "/api/acp"), ("GET", "/api/acp/activity"), ("GET", "/api/acp/folders"),
    *[("POST", "/api/acp/" + action) for action in ("sessions", "permission", "policy", "cancel", "handoff", "takeback", "retry",
                                                    "pick-folder")]])
def test_acp_endpoints_require_token(server, method, path):
    srv, _ = server
    status, _ = _get(_base(srv) + path) if method == "GET" else _post(_base(srv) + path, b"{}")
    assert status == 403


def test_create_rejects_missing_directory_before_identity_creation(server, home):
    status, result = post(server, "/api/acp/sessions", {"cwd": str(home / "missing")})
    assert status == 400 and not result["ok"]
    assert not db.list_acp()


def test_first_task_is_user_message_and_wakes_on_first_detector_scan(server, home):
    status, result = post(server, "/api/acp/sessions", {"backend": "agy", "cwd": str(home), "task": "initial task"})
    assert status == 200 and result["ok"]
    record = db.list_acp()[0]
    with db._conn() as connection:
        message = connection.execute("SELECT * FROM messages WHERE text='initial task'").fetchone()
        assert message["sender_id"] == db.user_id()
        assert json.loads(message["to_ids"]) == [record["id"]]
    calls = service.run_once()
    assert len(calls) == 1 and calls[0][0].agent_id == record["id"] and calls[0][1] is None
    host, sid = server[0].acp._sessions[record["id"]]
    until(lambda: host.sessions[sid].worker is None)
    assert "kopitiam" in str(host.sessions[sid].activity.snapshot())
    assert get(server, "/api/state")[1]["acp"]["sessions"][result["agent"]]["state"] == "idle"


def test_permission_cards_activity_options_and_notification(server, home, toasts):
    record = server[0].acp.create(str(home))
    future = server[0].acp.wake(record["id"], "permission")
    host, sid = server[0].acp._sessions[record["id"]]
    card = permission(host, sid)
    snapshot = get(server, "/api/acp")[1]["sessions"][record["name"]]
    assert snapshot["state"] == "waiting_permission"
    assert snapshot["permissions"][0]["toolCall"]["rawInput"]["CommandLine"] == "fake-only"
    until(lambda: any("等你确认" in str(toast) for toast in toasts))
    assert post(server, "/api/acp/permission", {"agent": record["name"], "request": card["request"], "option": "unknown"})[0] == 400
    assert post(server, "/api/acp/permission", {"agent": record["name"], "request": card["request"], "option": "once"})[0] == 200
    assert future.result(5)["stopReason"] == "end_turn"
    status, activity = get(server, "/api/acp/activity?agent=" + record["name"] + "&after=0")
    assert status == 200 and activity["events"] and activity["seq"] > 0
    assert get(server, "/api/acp/activity?agent=" + record["name"] + "&after=-1")[0] == 400


def test_handoff_requires_idle_and_takeback_requires_unlocked_presence(server, home, monkeypatch):
    record = server[0].acp.create(str(home))
    future = server[0].acp.wake(record["id"], "permission")
    host, sid = server[0].acp._sessions[record["id"]]
    permission(host, sid)
    assert post(server, "/api/acp/handoff", {"agent": record["name"]})[0] == 400
    assert post(server, "/api/acp/cancel", {"agent": record["name"]})[0] == 200
    assert future.result(5)["stopReason"] == "cancelled"
    status, out = post(server, "/api/acp/handoff", {"agent": record["name"]})
    assert status == 200 and "-LiteralPath" in out["commands"][0]
    assert sid in out["commands"][1]
    assert db.get_acp(record["id"])["handed_off"]
    assert get(server, "/api/acp")[1]["sessions"][record["name"]]["state"] == "handed_off"
    monkeypatch.setattr(managed, "presence_locked", lambda sid: True)
    assert post(server, "/api/acp/takeback", {"agent": record["name"]})[0] == 400
    assert db.get_acp(record["id"])["handed_off"]
    monkeypatch.setattr(managed, "presence_locked", lambda sid: False)
    assert post(server, "/api/acp/takeback", {"agent": record["name"]})[0] == 200
    assert db.get_acp(record["id"])["handed_off"] is None


def test_policy_and_all_acp_writes_respect_restore_gate(server, home):
    record = server[0].acp.create(str(home))
    assert post(server, "/api/acp/policy", {"agent": record["name"], "policy": "all"})[0] == 200
    assert db.get_acp(record["id"])["policy"] == "all"
    assert post(server, "/api/acp/policy", {"agent": record["name"], "policy": "invalid"})[0] == 400
    server[0].write_gate.close_and_wait()
    for path in ("sessions", "permission", "policy", "cancel", "handoff", "takeback", "retry"):
        assert post(server, "/api/acp/" + path, {"agent": record["name"]})[0] == 503


def test_retry_resumes_reapplies_options_and_only_rearms_detector(server, home, monkeypatch):
    runtime = server[0].acp
    record = runtime.create(str(home), options={"model": "fake", "effort": "low"})
    host, sid = runtime._sessions[record["id"]]
    with pytest.raises(Exception):
        runtime.wake(record["id"], "auth").result(5)
    with db._conn() as connection:
        connection.execute("UPDATE wake_state SET woken_pid=123 WHERE agent_id=?", (record["id"],))
    called = []
    request = host._client.request
    def track(method, params, **kwargs):
        called.append(method)
        return request(method, params, **kwargs)
    monkeypatch.setattr(host._client, "request", track)
    assert post(server, "/api/acp/retry", {"agent": record["name"]})[0] == 200
    assert called == ["session/resume", "session/set_config_option", "session/set_config_option"]
    assert runtime.state(record)["state"] == "idle"
    with db._conn() as connection:
        assert connection.execute("SELECT woken_pid FROM wake_state WHERE agent_id=?", (record["id"],)).fetchone()[0] is None
    monkeypatch.setattr(managed, "presence_locked", lambda sid: True)
    assert post(server, "/api/acp/retry", {"agent": record["name"]})[0] == 400
    monkeypatch.setattr(managed, "presence_locked", lambda sid: False)
    future = runtime.wake(record["id"], "permission")
    permission(host, sid)
    assert post(server, "/api/acp/retry", {"agent": record["name"]})[0] == 400
    runtime.cancel_agent(record["name"])
    future.result(5)
    until(lambda: host.sessions[sid].worker is None)
    runtime.handoff(record["name"])
    assert post(server, "/api/acp/retry", {"agent": record["name"]})[0] == 400
    failed_id = db.create_acp(str(home))
    runtime.errors[failed_id] = "creation failed"
    assert post(server, "/api/acp/retry", {"agent": db.get_acp(failed_id)["name"]})[0] == 400


def test_folder_suggestions_list_existing_project_dirs(server, home, monkeypatch):
    from kopitiam.acp import folders
    old, new, gone = home / "old-project", home / "new-project", home / "deleted-project"
    old.mkdir()
    new.mkdir()
    monkeypatch.setattr(folders.os.path, "expanduser", lambda p: str(home))  # 家目录本身不算项目
    with db._conn() as c:
        for name, path, seen in (("a", old, "2026-11-01T00:00:00"), ("b", new, "2026-11-02T00:00:00"),
                                 ("c", gone, "2026-11-03T00:00:00"), ("d", home, "2026-11-04T00:00:00")):
            c.execute("INSERT INTO agents(name, kind, cwd, launch_cwd, last_read, created_at, last_seen)"
                      " VALUES(?, 'claude', ?, ?, 0, ?, ?)", (name, str(path), str(path), seen, seen))
        c.execute("INSERT INTO agents(name, kind, cwd, launch_cwd, last_read, created_at, last_seen, trashed_at)"
                  " VALUES('e', 'codex', ?, ?, 0, 'x', '2026-11-05T00:00:00', 'x')", (str(old), str(old)))
    status, result = get(server, "/api/acp/folders")
    # 最近用过的在前；不存在的、家目录、回收站里的身份都不列；同一个文件夹只列一次
    assert status == 200 and result["folders"] == [str(new), str(old)]


def test_pick_folder_returns_choice_without_runtime(server, home, monkeypatch):
    from kopitiam.acp import folders
    seen = []
    monkeypatch.setattr(folders, "run_picker", lambda initial: seen.append(initial) or "C:/Projects/shop-web")
    server[0].acp.close()  # 选文件夹不依赖 ACP 托管
    status, result = post(server, "/api/acp/pick-folder", {"initial": r"  C:\Projects  "})
    assert status == 200 and result["path"] == r"C:\Projects\shop-web" and seen == [r"C:\Projects"]
    monkeypatch.setattr(folders, "run_picker", lambda initial: "")  # 用户点了取消
    assert post(server, "/api/acp/pick-folder", {})[1] == {"ok": True, "path": ""}


def test_only_one_folder_picker_at_a_time(server, monkeypatch):
    from kopitiam.acp import folders
    opened, release = threading.Event(), threading.Event()
    def slow(initial):
        opened.set()
        assert release.wait(10)
        return "C:/Projects/a"
    monkeypatch.setattr(folders, "run_picker", slow)
    first = threading.Thread(target=post, args=(server, "/api/acp/pick-folder", {}))
    first.start()
    try:
        assert opened.wait(5)
        status, result = post(server, "/api/acp/pick-folder", {})
        assert status == 400 and "已经开着" in result["message"]
    finally:
        release.set()
        first.join(10)
    assert post(server, "/api/acp/pick-folder", {})[1]["path"] == r"C:\Projects\a"


def test_picker_script_compiles():
    from kopitiam.acp import folders
    compile(folders.PICK_SCRIPT, "<pick-folder>", "exec")
