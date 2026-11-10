import json
import threading
import time

import pytest

from kopitiam import backup, db, web
from test_web import _base, _get, _post


@pytest.fixture
def server(home, dav, monkeypatch):
    monkeypatch.setattr(backup, "other_processes", lambda: [])
    srv, token, url = web.make_server(port=0, peer_host="127.0.0.1", peer_port=0, share=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    backup.store.save(dict(url=dav.url, user="tester", password="application-password", backup_password="private-backup-secret"))
    yield srv, token, url
    if srv.backup._thread:
        srv.backup._thread.join(20)
    srv.shutdown()
    srv.server_close()
    thread.join()


def post(server, path, value=None):
    srv, token, _ = server
    code, raw = _post(_base(srv) + path, json.dumps(value or {}).encode(), {"X-Token": token})
    return code, json.loads(raw)


def get(server, path):
    srv, token, _ = server
    code, raw = _get(_base(srv) + path, {"X-Token": token})
    return code, json.loads(raw)


@pytest.mark.parametrize("method,path", [("GET", "/api/backup"), ("GET", "/api/backup/list"),
    ("POST", "/api/backup/config"), ("POST", "/api/backup/test"), ("POST", "/api/backup/run"),
    ("POST", "/api/backup/restore"), ("POST", "/api/backup/delete")])
def test_backup_requires_token(server, method, path):
    url = _base(server[0]) + path
    code, _ = _get(url) if method == "GET" else _post(url, b"{}")
    assert code == 403


def test_api_no_secrets_settings_and_jobs(server):
    code, state = get(server, "/api/backup")
    assert code == 200 and state["config"]["has_password"]
    assert "application-password" not in json.dumps(state) and "private-backup-secret" not in json.dumps(state)
    assert post(server, "/api/backup/config", {"password":"", "backup_password":""})[1]["ok"]
    assert post(server, "/api/backup/config", {"url":"http://example.com"})[0] == 400
    assert post(server, "/api/backup/test")[1]["count"] == 0
    assert post(server, "/api/backup/run", {"files":"yes"})[0] == 400
    assert post(server, "/api/backup/run", {"files":False})[1]["ok"]
    manager = server[0].backup
    manager._thread.join(20)
    assert manager.status()["job"]["state"] == "done"
    entries = get(server, "/api/backup/list")[1]
    assert len(entries) == 1
    assert post(server, "/api/backup/delete", {"name":"../escape"})[0] == 400
    assert post(server, "/api/backup/delete", {"name":entries[0]["name"]})[1]["ok"]
    assert get(server, "/api/backup/list")[1] == []


def test_concurrent_api_jobs_refused(server, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = backup._pack
    def wait(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(backup, "_pack", wait)
    assert post(server, "/api/backup/run")[1]["ok"]
    assert entered.wait(5)
    try:
        assert post(server, "/api/backup/run")[0] == 409
        assert post(server, "/api/backup/config", {"folder":"other"})[0] == 409
        code, state = get(server, "/api/backup")
        assert code == 200 and state["job"]["state"] == "running"
    finally:
        release.set()
        server[0].backup._thread.join(20)


def test_restore_blocks_web_peer_writes_and_allows_password_retry(server, monkeypatch):
    srv = server[0]
    assert post(server, "/api/backup/run")[1]["ok"]
    srv.backup._thread.join(20)
    name = srv.backup.status()["job"]["name"]
    entered, release = threading.Event(), threading.Event()
    # 密码错时尚未进入写回，不影响网页或团队服务。
    assert post(server, "/api/backup/restore", {"name":name, "backup_password":"wrong"})[1]["ok"]
    srv.backup._thread.join(20)
    assert srv.backup.status()["job"]["needs_password"]
    assert not srv.backup.read_only and not srv.peer_service.read_only
    assert srv.peer_service._worker.is_alive()
    assert post(server, "/api/send", {"text":"密码错后仍能发消息"})[1]["ok"]
    original = backup._apply_restore
    def wait(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(backup, "_apply_restore", wait)
    assert post(server, "/api/backup/restore", {"name":name, "backup_password":"private-backup-secret"})[1]["ok"]
    assert entered.wait(5)
    try:
        for path in ["/api/send", "/api/upload", "/api/forget", "/api/groups", "/api/seen", "/api/wake/pause", "/api/team/share", "/api/archive"]:
            code, out = post(server, path)
            assert code == 503 and out["message"] == backup.READ_ONLY
        peer_url = f"http://127.0.0.1:{srv.peer_service.bound_port}"
        assert _get(peer_url + "/peer/hello")[0] == 503
        for path in ["/peer/ping", "/peer/message", "/peer/file", "/peer/pair", "/peer/unpair"]:
            assert _post(peer_url + path, b"{}")[0] == 503
        assert not srv.peer_service._worker.is_alive()
        assert get(server, "/api/backup")[1]["restart_required"]
    finally:
        release.set()
        srv.backup._thread.join(20)
    assert srv.backup.status()["job"]["state"] == "done"
    assert get(server, "/api/state")[1]["restart_required"]
    assert post(server, "/api/send")[0] == 503
    assert post(server, "/api/backup/run")[0] == 503


def test_restore_waits_for_existing_web_write(server, monkeypatch):
    srv = server[0]
    assert post(server, "/api/backup/run")[1]["ok"]
    srv.backup._thread.join(20)
    name = srv.backup.status()["job"]["name"]
    entered, release, applied, pausing = (threading.Event() for _ in range(4))
    original_post, original_apply = web._Handler._post, backup._apply_restore
    original_pause = srv.backup.on_restore
    def pause():
        pausing.set()
        original_pause()
    srv.backup.on_restore = pause
    def blocked(handler):
        entered.set()
        assert release.wait(10)
        return original_post(handler)
    def apply(*args):
        applied.set()
        return original_apply(*args)
    monkeypatch.setattr(web._Handler, "_post", blocked)
    monkeypatch.setattr(backup, "_apply_restore", apply)
    writer = threading.Thread(target=lambda: post(server, "/api/send", {"text":"已有请求先写完"}))
    writer.start()
    assert entered.wait(5)
    try:
        assert post(server, "/api/backup/restore", {"name":name})[1]["ok"]
        assert pausing.wait(5)
        assert not applied.is_set()
        assert get(server, "/api/backup")[1]["job"]["state"] == "running"
        assert post(server, "/api/send", {"text":"关闸后拒绝"})[0] == 503
    finally:
        release.set()
        writer.join(10)
        srv.backup._thread.join(20)
    assert applied.is_set() and srv.backup.status()["job"]["state"] == "done"


def test_wake_pause_drains_current_round(monkeypatch):
    from kopitiam.wake import service
    db.init_db()
    entered, release, paused = threading.Event(), threading.Event(), threading.Event()
    def round():
        entered.set()
        assert release.wait(10)
        db.set_meta("test-round", "finished")
        return []
    monkeypatch.setattr(service, "run_once", round)
    wake = service.WakeService(interval=.01)
    wake.start()
    assert entered.wait(5)
    waiter = threading.Thread(target=lambda: (wake.pause_for_restore(), paused.set()))
    waiter.start()
    try:
        time.sleep(.1)
        assert not paused.is_set()
    finally:
        release.set()
        waiter.join(5)
    assert paused.is_set() and not wake._thread.is_alive()
    assert db.get_meta("test-round") == "finished"


def test_blocked_download_does_not_block_state_or_posts(server, monkeypatch):
    srv, token, _ = server
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    outcomes = []
    def slow_file(handler, name):
        entered.set()
        assert release.wait(10)
        handler._send_bytes(b"attachment", "application/octet-stream")
    monkeypatch.setattr(web._Handler, "_serve_file", slow_file)
    download = threading.Thread(target=lambda: _get(_base(srv) + "/files/test.txt?t=" + token))
    download.start()
    assert entered.wait(5)
    def other_requests():
        outcomes.append(get(server, "/api/state")[0])
        outcomes.append(post(server, "/api/send", {"text":"下载时仍能发消息"})[0])
        finished.set()
    other = threading.Thread(target=other_requests)
    other.start()
    try:
        assert finished.wait(3), "下载不应让状态轮询或发送排队"
        assert outcomes == [200, 200]
    finally:
        release.set()
        download.join(10)
        other.join(10)


def test_posts_remain_concurrent_until_gate_closes(server, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = web._Handler._post
    def slow_post(handler):
        if handler.path == "/api/forget":
            entered.set()
            assert release.wait(10)
            return handler._json({"ok":True})
        return original(handler)
    monkeypatch.setattr(web._Handler, "_post", slow_post)
    slow = threading.Thread(target=lambda: post(server, "/api/forget"))
    slow.start()
    assert entered.wait(5)
    outcomes = []
    def quick_post():
        outcomes.append(post(server, "/api/send", {"text":"并发写请求"})[0])
        finished.set()
    quick = threading.Thread(target=quick_post)
    quick.start()
    try:
        assert finished.wait(3), "POST 之间不应互相串行"
        assert outcomes == [200]
        with pytest.raises(ValueError, match="已有写入"):
            server[0].write_gate.close_and_wait(timeout=.01)
        assert post(server, "/api/send", {"text":"关闸后"})[0] == 503
    finally:
        release.set()
        slow.join(10)
        quick.join(10)
    server[0].write_gate.close_and_wait(timeout=.01)
