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
