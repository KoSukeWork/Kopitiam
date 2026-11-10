"""web 测试：线程里起服务（端口 0 随机），直接打 HTTP 验证 §8.1/§8.2。"""
from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kopitiam import config, web
from kopitiam import db


@pytest.fixture()
def server(home):
    srv, token, url = web.make_server(port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, token, url
    srv.shutdown()
    srv.server_close()


def _base(srv) -> str:
    port = srv.server_address[1]
    return f"http://127.0.0.1:{port}"


def _get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(url: str, body: bytes, headers: dict | None = None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.mark.parametrize("path, method", [("/api/groups", "POST"), ("/api/group", "POST"),
    ("/api/seen", "POST"), ("/api/send", "POST"), ("/api/messages?group=1", "GET")])
def test_group_endpoints_require_token(server, path, method):
    srv, token, _ = server
    if method == "POST":
        code, _ = _post(_base(srv) + path, b'{"group": 1}')
    else:
        code, _ = _get(_base(srv) + path)
    assert code == 403


def test_group_web_lifecycle(server, home):
    import os
    srv, token, _ = server
    headers = {"X-Token": token}
    def post(path, body):
        status, raw = _post(_base(srv) + path, json.dumps(body).encode(), headers)
        assert status == 200
        return json.loads(raw)
    def get(path):
        status, raw = _get(_base(srv) + path, headers)
        assert status == 200
        return json.loads(raw)
    a = db.claim("claude", cwd=str(home), name="api", pid=os.getpid())
    b = db.claim("codex", cwd=str(home), name="codex@web", pid=os.getpid())
    made = post("/api/groups", {"title": "分页", "about": "接上接口", "members": ["api"]})
    assert made["ok"]
    g = made["group"]["id"]
    assert set(made["group"]["members"]) == {"api", "user"}
    assert post("/api/send", {"group": g, "text": "@codex@web你好"})["ok"]
    assert not post("/api/send", {"group": g, "text": "bad", "to": []})["ok"]
    db.Chat().send(a, "@codex@web ready", group=g)
    msgs = get(f"/api/messages?group={g}")
    assert len(msgs["messages"]) == 2 and len(msgs["events"]) == 3
    assert msgs["messages"][-1]["group"] == g and not msgs["messages"][-1]["to_all"]
    state = get("/api/state")
    assert state["groups"][0]["unread"] == 1
    assert state["groups"][0]["count"] == 2
    assert post("/api/seen", {"group": g, "upto": msgs["messages"][-1]["id"]})["ok"]
    assert get("/api/state")["groups"][0]["unread"] == 0
    assert post("/api/seen", {"group": g, "upto": 0})["ok"]  # 进度不能倒退
    assert get("/api/state")["groups"][0]["unread"] == 0
    changed = post("/api/group", {"id": g, "title": "分页完成", "about": "已验证", "remove": ["api"], "archived": True})
    assert changed["ok"] and changed["group"]["archived_at"]
    assert not post("/api/send", {"group": g, "text": "archived"})["ok"]
    assert post("/api/group", {"id": g, "archived": False, "add": ["api"]})["ok"]
    assert post("/api/send", {"group": g, "text": "继续"})["ok"]
    assert post("/api/groups", {"title": "空群"})["ok"]
    assert "restored" in [e["kind"] for e in get(f"/api/messages?group={g}")["events"]]
    assert post("/api/send", {"text": "legacy broadcast", "to": []})["ok"]
    assert get("/api/state")["messages"][-1]["group"] is None


def test_group_web_validation_is_atomic(server, home):
    srv, token, _ = server
    headers = {"X-Token": token}
    remote = db.add_peer("remote", "peer", "example:123", "secret")
    for body in ({"title": ""}, {"title": "x" * 41}, {"title": "ok", "about": "x" * 2001},
                 {"title": "ok", "members": [remote["alias"]]}, {"title": "ok", "members": ["user"]},
                 {"title": "ok", "members": "peer"}):
        status, raw = _post(_base(srv) + "/api/groups", json.dumps(body).encode(), headers)
        assert status == 200 and not json.loads(raw)["ok"]
    assert db.Chat().groups_brief() == []
    g = db.Chat().create_group(db.user_id(), "original")["id"]
    for body in ({"id": g, "title": "changed", "remove": ["user"]},
                 {"id": g, "archived": "false"}, {"id": g, "add": [remote["alias"]]}, {"id": "bad"}):
        _, raw = _post(_base(srv) + "/api/group", json.dumps(body).encode(), headers)
        assert not json.loads(raw)["ok"]
    assert db.Chat().groups_brief()[0]["title"] == "original"
    for query in ("bad", "0", "", "999"):
        status, _ = _get(_base(srv) + "/api/messages?group=" + query, headers)
        assert status == 400


def test_group_timeline_batches_only_consecutive_invites():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed to exercise the inline timeline function")
    function = "function timelineItems(" + web.INDEX_HTML.split("function timelineItems(", 1)[1].split("async function loadGroupHistory", 1)[0]
    def event(i, second, actor="user", kind="added"):
        return {"id": i, "time": "2026-11-10T11:" + second, "actor": actor, "kind": kind, "agent": f"agent-{i}"}
    events = [event(1, "09:00", kind="created"), event(2, "09:01"), event(3, "09:02"),
              event(4, "09:04"), event(5, "09:05", "frontend"), event(6, "10:00", "frontend"),
              event(7, "10:01", None), event(8, "10:02", None)]
    messages = [{"id": 1, "time": "2026-11-10T11:09:03"}]
    script = function + "\nconsole.log(JSON.stringify(timelineItems(" + json.dumps(messages) + "," + json.dumps(events) + ")));"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    items = json.loads(result.stdout)
    assert len(items) == 8
    assert items[1]["data"]["agents"] == ["agent-2", "agent-3"]
    assert items[2]["event"] is False  # 中间有消息，之后的加入不能并到前面。
    assert [x["data"].get("agents") for x in items[3:]] == [[f"agent-{i}"] for i in range(4, 9)]


def test_get_page_no_token_needed(server):
    srv, token, url = server
    code, body = _get(_base(srv) + "/")
    assert code == 200
    assert "kopitiam" in body.decode("utf-8")


def test_api_state_requires_token(server):
    srv, token, url = server
    base = _base(srv)
    code, _ = _get(base + "/api/state?after=0")
    assert code == 403  # 没有 token
    code, _ = _get(base + "/api/state?after=0", headers={"X-Token": "wrong"})
    assert code == 403  # token 错误


def test_wrong_host_403(server):
    srv, token, url = server
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("GET", "/api/state?after=0", skip_host=True)
    conn.putheader("Host", "evil.example.com:1234")  # Host 不对
    conn.putheader("X-Token", token)
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 403
    resp.read()
    conn.close()


def test_files_traversal_404(server):
    srv, token, url = server
    # init_db 已建好 FILES_DIR 和 chat.db；/files/ 只能访问 FILES_DIR 里面的文件
    code, _ = _get(_base(srv) + f"/files/..%2Fchat.db?t={token}")
    assert code == 404
    code, _ = _get(_base(srv) + "/files/..%2F..%2Fpyproject.toml?t=" + token)
    assert code == 404


def test_files_serves_text_plain(server, home):
    srv, token, url = server
    from urllib.parse import quote

    f = config.files_dir() / "说明.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# 你好", encoding="utf-8")
    code, body = _get(_base(srv) + f"/files/{quote(f.name)}?t={token}")
    assert code == 200
    assert body == "# 你好".encode("utf-8")
    # 没有 t 参数的 /files/ 也要 403
    code, _ = _get(_base(srv) + f"/files/{quote(f.name)}")
    assert code == 403


def test_send_then_state_returns_message(server):
    srv, token, url = server
    base = _base(srv)
    code, body = _post(
        base + "/api/send",
        json.dumps({"text": "网页发言", "to": []}).encode("utf-8"),
        headers={"X-Token": token},
    )
    assert code == 200
    out = json.loads(body)
    assert out["ok"] is True
    code, body = _get(base + "/api/state?after=0", headers={"X-Token": token})
    assert code == 200
    state = json.loads(body)
    assert any(m["text"] == "网页发言" for m in state["messages"])
    assert any(a["name"] == "user" for a in state["agents"])
    # after 过滤生效
    last = state["messages"][-1]["id"]
    code, body = _get(base + f"/api/state?after={last}", headers={"X-Token": token})
    state2 = json.loads(body)
    assert state2["messages"] == []


def test_send_with_attachment_url(server, home):
    srv, token, url = server
    src = Path(home) / "说明.txt"
    src.write_text("附件内容", encoding="utf-8")
    code, body = _post(
        _base(srv) + "/api/send",
        json.dumps({"text": "带附件", "to": [], "files": [str(src)]}).encode("utf-8"),
        headers={"X-Token": token},
    )
    out = json.loads(body)
    assert out["ok"] is True
    code, body = _get(_base(srv) + "/api/state?after=0", headers={"X-Token": token})
    state = json.loads(body)
    msg = next(m for m in state["messages"] if m["text"] == "带附件")
    assert msg["files"] and msg["files"][0]["url"].startswith("/files/")
    assert "t=" in msg["files"][0]["url"]
    assert msg["files"][0]["name"] == "说明.txt"


def test_upload_and_download(server):
    srv, token, url = server
    from urllib.parse import quote

    data = "上传的字节内容".encode("utf-8")
    code, body = _post(
        _base(srv) + "/api/upload?name=" + quote("报告.txt"),
        data,
        headers={"X-Token": token, "Content-Type": "application/octet-stream"},
    )
    assert code == 200
    out = json.loads(body)
    assert out["ok"] is True
    p = Path(out["path"])
    assert p.parent == config.files_dir()
    assert p.read_bytes() == data
    # 上传的名字要被 safe() 清洗：路径分隔符必须消失，不能逃出 FILES_DIR
    code, body = _post(
        _base(srv) + "/api/upload?name=%2E%2E%2Fevil.txt",
        b"x",
        headers={"X-Token": token, "Content-Type": "application/octet-stream"},
    )
    out = json.loads(body)
    name = Path(out["path"]).name
    assert "/" not in name and "\\" not in name
    assert Path(out["path"]).parent == config.files_dir()


def test_hidden_forget_and_history_endpoints(server):
    import os
    from datetime import datetime, timedelta

    from kopitiam import db

    srv, token, url = server
    base, h = _base(srv), {"X-Token": token}
    old = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
    a = db.claim("codex", cwd=".", name="old", pid=os.getpid())
    db.release(a)
    with db._conn() as c:
        c.execute("UPDATE agents SET last_seen=? WHERE id=?", (old, a))

    assert _get(base + "/api/hidden")[0] == 403  # 要 token
    code, body = _get(base + "/api/hidden", headers=h)
    assert code == 200
    assert [x["name"] for x in json.loads(body)["agents"]] == ["old"]

    _post(base + "/api/send", json.dumps({"text": "给 old", "to": ["old"]}).encode("utf-8"), h)
    code, body = _get(base + "/api/messages?with=old", headers=h)
    assert [m["text"] for m in json.loads(body)["messages"]] == ["给 old"]

    code, body = _post(base + "/api/forget", json.dumps({"name": "old"}).encode("utf-8"), h)
    assert json.loads(body)["ok"] is False  # 有消息，不能删
    b = db.claim("pi", cwd=".", name="empty", pid=os.getpid())
    db.release(b)
    code, body = _post(base + "/api/forget", json.dumps({"name": "empty"}).encode("utf-8"), h)
    assert json.loads(body)["ok"] is True
    assert _post(base + "/api/forget", b"{}")[0] == 403  # 要 token


def test_port_in_use_gives_a_clear_error(home):
    import socket

    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    try:
        with pytest.raises(SystemExit) as e:  # 以前在这里先崩出 AttributeError，看不到真正的原因
            web.make_server(busy.getsockname()[1])
        assert "被占用" in str(e.value) and "--port" in str(e.value)
    finally:
        busy.close()


def test_archive_endpoint_moves_agent_to_hidden_and_back(server):
    import os

    from kopitiam import db

    srv, token, url = server
    base, h = _base(srv), {"X-Token": token}
    db.claim("codex", cwd=".", name="busy", pid=os.getpid())  # 在线的也能归档（桌面版的对话身份一直在线）

    def state():
        brief = {a["name"]: a for a in json.loads(_get(base + "/api/state", headers=h)[1])["agents"]}
        hidden = [a["name"] for a in json.loads(_get(base + "/api/hidden", headers=h)[1])["agents"]]
        return brief["busy"], hidden

    assert _post(base + "/api/archive", json.dumps({"name": "busy"}).encode("utf-8"))[0] == 403  # 要 token
    out = json.loads(_post(base + "/api/archive", json.dumps({"name": "busy", "archived": True}).encode("utf-8"), h)[1])
    assert out["ok"] and out["message"] == "已归档 busy"
    a, hidden = state()
    assert a["archived"] and a["hidden"] and a["online"] and hidden == ["busy"]
    out = json.loads(_post(base + "/api/archive", json.dumps({"name": "busy", "archived": False}).encode("utf-8"), h)[1])
    assert out["ok"]
    a, hidden = state()
    assert not a["archived"] and not a["hidden"] and hidden == []
    out = json.loads(_post(base + "/api/archive", json.dumps({"name": "user"}).encode("utf-8"), h)[1])
    assert not out["ok"]  # 用户身份不能归档


def test_trash_and_purge_endpoints(server):
    import os

    from kopitiam import db

    srv, token, url = server
    base, h = _base(srv), {"X-Token": token}
    a = db.claim("codex", cwd=".", name="noisy", pid=os.getpid())
    db.Chat().send(a, "测试消息", ["user"])

    def post(path, body):
        return json.loads(_post(base + path, json.dumps(body).encode("utf-8"), h)[1])

    out = post("/api/trash", {"name": "noisy", "trashed": True})
    assert out["ok"] and out["message"].startswith("已移到回收站：noisy")
    hidden = {x["name"]: x for x in json.loads(_get(base + "/api/hidden", headers=h)[1])["agents"]}
    assert hidden["noisy"]["trashed_at"] and hidden["noisy"]["purge_at"] > hidden["noisy"]["trashed_at"]
    assert post("/api/purge", {"name": "noisy"})["ok"]
    assert "noisy" not in {x["name"] for x in json.loads(_get(base + "/api/hidden", headers=h)[1])["agents"]}
    assert not post("/api/purge", {"name": "user"})["ok"]
    assert _post(base + "/api/trash", b"{}")[0] == 403  # 要 token


def test_open_attachment_endpoint(server, monkeypatch):
    srv, token, url = server
    base, h = _base(srv), {"X-Token": token}
    opened = []
    monkeypatch.setattr(web, "open_local", lambda path, folder=False: opened.append((path.name, folder)))
    config.files_dir().mkdir(parents=True, exist_ok=True)
    (config.files_dir() / "000001_0_plan.md").write_text("计划", encoding="utf-8")
    (config.files_dir() / "000002_0_run.bat").write_text("echo hi", encoding="utf-8")

    def post(body, headers=h):
        code, raw = _post(base + "/api/open", json.dumps(body).encode("utf-8"), headers)
        return code, (json.loads(raw) if code == 200 else None)

    assert post({"name": "000001_0_plan.md"}, {})[0] == 403  # 要 token
    assert post({"name": "000001_0_plan.md"})[1] == {"ok": True, "message": "已打开"}
    assert post({"name": "000001_0_plan.md", "folder": True})[1]["message"] == "已打开文件夹"
    out = post({"name": "000002_0_run.bat"})[1]  # 打开就会直接运行的：只在文件夹里选中
    assert out["ok"] and out["revealed"] and "为了安全" in out["message"]
    for bad in ("../chat.db", "..", "", "nope.md"):  # 只能打开附件目录里的文件
        assert post({"name": bad})[1]["ok"] is False
    assert opened == [("000001_0_plan.md", False), ("000001_0_plan.md", True), ("000002_0_run.bat", True)]
