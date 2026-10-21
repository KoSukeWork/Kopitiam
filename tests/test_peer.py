"""团队（跨电脑）测试：签名、地址、本机规则，以及两个 kopitiam 之间真实的配对和收发。

另一台电脑用子进程模拟（它有自己的 KOPITIAM_HOME），两边的团队端口都只绑 127.0.0.1。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from kopitiam import config, db, peer, web


# ---- 单元：签名、地址 ----


def test_sign_and_verify():
    ts = str(int(time.time()))
    s = peer.sign("k", "POST", "/peer/message", ts, b"body")
    assert peer.verify("k", "POST", "/peer/message", ts, b"body", s)
    assert not peer.verify("k", "POST", "/peer/message", ts, b"tampered", s)  # 内容被改
    assert not peer.verify("other", "POST", "/peer/message", ts, b"body", s)  # 密钥不对
    assert not peer.verify("k", "POST", "/peer/ping", ts, b"body", s)  # 换了接口
    old = str(int(time.time()) - 3600)
    assert not peer.verify("k", "POST", "/peer/message", old, b"body",
                           peer.sign("k", "POST", "/peer/message", old, b"body"))  # 太旧，防重放
    assert not peer.verify("k", "POST", "/peer/message", "abc", b"body", s)


def test_normalize_address():
    assert peer.normalize_address("192.168.1.5") == f"192.168.1.5:{config.PEER_PORT}"
    assert peer.normalize_address(" http://192.168.1.5:9000/ ") == "192.168.1.5:9000"
    with pytest.raises(peer.PeerError):
        peer.normalize_address("")
    with pytest.raises(peer.PeerError):
        peer.normalize_address("1.2.3.4:99999")


# ---- 本机规则（不用真的连别人） ----


def _fake_peer(name="bob") -> tuple[dict, str]:
    node = uuid.uuid4().hex
    return db.add_peer(node, name, "127.0.0.1:1", "secret"), node


def test_agents_cannot_send_to_remote_and_who_hides_them(home):
    db.init_db()
    info, _ = _fake_peer()
    agent = db.claim("claude", cwd=str(home), name="A", pid=os.getpid())
    chat = db.Chat()
    out = chat.send(agent, "把日志发给 bob", to=[info["alias"]])
    assert out.startswith("错误") and "另一台电脑" in out
    assert info["alias"] not in [ln.split()[1] for ln in chat.who().splitlines()]
    # 用户可以发，进发件箱
    out = chat.send(db.user_id(), "hi", to=[info["alias"]])
    assert out.startswith("已发送")
    assert len(db.due_outbox()) == 1


def test_mcp_cannot_claim_remote_identity(home):
    db.init_db()
    info, _ = _fake_peer("bob")
    i = db.claim("codex", cwd=str(home), name="bob", pid=os.getpid())
    assert i != info["agent_id"]  # 名字被同事占着，agent 拿到的是 bob-2
    assert db.Chat().whoami(i).startswith("你是 bob-2")


def test_receive_remote_is_deduped_and_only_for_user(home):
    db.init_db()
    info, node = _fake_peer()
    agent = db.claim("claude", cwd=str(home), name="A", pid=os.getpid())
    assert db.receive_remote(node, f"{node}:1", "你好", []) is not None
    assert db.receive_remote(node, f"{node}:1", "你好", []) is None  # 对方重发，不重复
    assert db.Chat().read(agent) == "没有新消息。"  # agent 看不到别人电脑发来的
    msgs = db.Chat().messages_after(0)
    assert [m["text"] for m in msgs] == ["你好"]
    assert msgs[0]["sender"] == info["alias"] and msgs[0]["sender_kind"] == "remote"


def test_forward_marks_origin_and_keeps_attachment_name(home):
    db.init_db()
    info, node = _fake_peer()
    staged = home / "staged.bin"
    staged.write_bytes(b"data")
    mid = db.receive_remote(node, f"{node}:7", "帮我看下这个报错", [(staged, "error.log")])
    agent = db.claim("claude", cwd=str(home), name="A", pid=os.getpid())
    chat = db.Chat()
    out = chat.forward(db.user_id(), mid, ["A"])
    assert out.startswith("已发送") and "附件 1 个" in out
    got = chat.read(agent)
    assert "转发自 bob · 另一台电脑" in got and "帮我看下这个报错" in got
    fwd = chat.messages_after(0)[-1]
    assert fwd["files"][0]["name"] == "error.log"


def test_paired_remote_not_purged_and_hidden_after_unpair(home):
    from datetime import datetime, timedelta

    db.init_db()
    info, _ = _fake_peer()
    old = (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
    with db._conn() as c:
        c.execute("UPDATE agents SET last_seen=? WHERE id=?", (old, info["agent_id"]))
    assert db.purge_stale() == 0  # 还在配对，没消息、很久没联系也不清理
    brief = {a["name"]: a for a in db.Chat().agents_brief()}
    assert brief[info["alias"]]["hidden"] is False
    db.remove_peer(db.list_peers()[0]["id"])
    brief = {a["name"]: a for a in db.Chat().agents_brief()}
    assert brief[info["alias"]]["hidden"] is True  # 解除配对后马上收进「已隐藏」


# ---- 两台「电脑」之间真实收发 ----


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _req(base: str, token: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET",
                                 headers={"X-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


@pytest.fixture()
def two_nodes(home):
    """A = 本进程里的网页；B = 子进程里的另一份 kopitiam（自己的数据目录）。"""
    b_home = Path(tempfile.gettempdir()) / f"kopitiam-peer-b-{uuid.uuid4().hex[:8]}"
    b_home.mkdir()
    b_web, b_peer = _free_port(), _free_port()
    env = dict(os.environ, KOPITIAM_HOME=str(b_home), PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "kopitiam.cli", "web", "--port", str(b_web), "--no-open",
         "--share", "--peer-port", str(b_peer), "--peer-host", "127.0.0.1"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True, encoding="utf-8",
    )
    line: list[str] = []
    t = threading.Thread(target=lambda: line.append(proc.stdout.readline()), daemon=True)
    t.start()
    t.join(20)
    assert line and "?t=" in line[0], f"B 没起来：{line}"
    b_token = line[0].strip().split("?t=")[1]

    a_peer = _free_port()
    srv, a_token, _ = web.make_server(port=0, peer_host="127.0.0.1", peer_port=a_peer, share=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    a = {"base": f"http://127.0.0.1:{srv.server_address[1]}", "token": a_token, "srv": srv}
    b = {"base": f"http://127.0.0.1:{b_web}", "token": b_token, "peer": f"127.0.0.1:{b_peer}"}
    try:
        yield a, b
    finally:
        srv.shutdown()
        srv.server_close()
        proc.terminate()
        proc.wait(10)
        shutil.rmtree(b_home, ignore_errors=True)


def _wait(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.3)
    return fn()


def test_pair_send_receive_forward_unpair(two_nodes, home):
    a, b = two_nodes
    A = lambda path, p=None: _req(a["base"], a["token"], path, p)  # noqa: E731
    B = lambda path, p=None: _req(b["base"], b["token"], path, p)  # noqa: E731
    assert A("/api/team/me", {"name": "alice"})["ok"]
    assert B("/api/team/me", {"name": "bob"})["ok"]

    # 配对码错了不行
    bad = A("/api/team/join", {"address": b["peer"], "code": "000000"})
    assert bad["ok"] is False
    # B 生成配对码，A 输入 B 的地址和码
    code = B("/api/team/code", {})["code"]
    out = A("/api/team/join", {"address": b["peer"], "code": code})
    assert out["ok"], out
    assert out["alias"] == "bob"
    assert [p["alias"] for p in B("/api/team")["peers"]] == ["alice"]
    assert B("/api/team/join", {"address": b["peer"], "code": code})["ok"] is False  # 配对码只能用一次

    # A → B，带附件
    src = home / "notes.txt"
    src.write_text("附件内容", encoding="utf-8")
    sent = A("/api/send", {"text": "hi bob", "to": ["bob"], "files": [str(src)]})
    assert sent["ok"], sent
    a["srv"].peer_service.deliver_due()
    got = _wait(lambda: [m for m in B("/api/state?after=0")["messages"] if m["text"] == "hi bob"])
    assert got, "B 没收到"
    m = got[0]
    assert m["sender"] == "alice" and m["sender_kind"] == "remote" and m["to"] == ["user"]
    assert m["files"][0]["name"] == "notes.txt" and m["files"][0]["size"] == src.stat().st_size
    st = A("/api/state?after=0")
    mine = next(x for x in st["messages"] if x["text"] == "hi bob")
    assert mine["delivery"]["state"] == "sent"

    # B → A（B 的后台线程负责投递）
    assert B("/api/send", {"text": "收到", "to": ["alice"]})["ok"]
    back = _wait(lambda: [x for x in A("/api/state?after=0")["messages"] if x["text"] == "收到"])
    assert back and back[0]["sender"] == "bob"

    # A 把 bob 的消息转发给本机 agent，agent 能读到，并且标明来源
    agent = db.claim("claude", cwd=str(home), name="helper", pid=os.getpid())
    fw = A("/api/forward", {"id": back[0]["id"], "to": ["helper"]})
    assert fw["ok"], fw
    assert "转发自 bob · 另一台电脑" in db.Chat().read(agent)

    # 解除配对：两边都没有了
    assert A("/api/team/remove", {"name": "bob"})["ok"]
    assert A("/api/team")["peers"] == []
    assert _wait(lambda: B("/api/team")["peers"] == [])


def test_joiner_without_team_connection_is_told_and_gets_messages_after_enabling(two_nodes):
    # 「我去加别人」的一方没开团队连接也能配对，但对方发给它的消息会一直排队：配对时就提醒，原因也写清楚
    a, b = two_nodes
    A = lambda path, p=None: _req(a["base"], a["token"], path, p)  # noqa: E731
    B = lambda path, p=None: _req(b["base"], b["token"], path, p)  # noqa: E731
    A("/api/team/me", {"name": "alice"})
    B("/api/team/me", {"name": "bob"})
    A("/api/team/share", {"on": False})
    out = A("/api/team/join", {"address": b["peer"], "code": B("/api/team/code", {})["code"]})
    assert out["ok"] and "你还没打开「团队连接」" in out["message"]

    assert B("/api/send", {"text": "在吗", "to": ["alice"]})["ok"]
    err = _wait(lambda: [d["error"] for d in B("/api/state?after=0")["deliveries"].values() if d["error"]])
    assert err and err[0] == peer.NOT_SHARING  # B 的网页上「等待送达」下面写着原因

    A("/api/team/share", {"on": True})
    a["srv"].peer_service.ping_all()  # A 的后台线程每 20 秒 ping 一次，带上新端口；B 记下以后马上重发
    got = _wait(lambda: [m for m in A("/api/state?after=0")["messages"] if m["text"] == "在吗"])
    assert got and got[0]["sender"] == "bob"


def test_peer_port_explains_why_it_rejects(two_nodes):
    a, b = two_nodes
    A = lambda path, p=None: _req(a["base"], a["token"], path, p)  # noqa: E731
    B = lambda path, p=None: _req(b["base"], b["token"], path, p)  # noqa: E731
    A("/api/team/join", {"address": b["peer"], "code": B("/api/team/code", {})["code"]})
    secret = db.list_peers()[0]["secret"]

    def ping(node, ts, key):
        body = b"{}"
        req = urllib.request.Request(f"http://{b['peer']}/peer/ping", data=body, method="POST", headers={
            "Content-Type": "application/json", "X-Kopitiam-Node": node, "X-Kopitiam-Time": str(ts),
            "X-Kopitiam-Sig": peer.sign(key, "POST", "/peer/ping", str(ts), body), "X-Kopitiam-Port": "0"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        return json.loads(ei.value.read())["error"]

    now = int(time.time())
    assert "没有你的配对记录" in ping(uuid.uuid4().hex, now, secret)
    assert ping(db.node_id(), now - 600, secret).startswith("两台电脑的时间相差 10 分钟")
    assert "签名不对" in ping(db.node_id(), now, "wrong-secret")

    # A 自己定时联系 B 被拒绝时，A 的团队面板上也看得到原因（以前悄悄失败）
    real = time.time
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(time, "time", lambda: real() - 600)  # A 的时钟慢了 10 分钟
        a["srv"].peer_service.ping_all()
    assert A("/api/team")["peers"][0]["error"].startswith("两台电脑的时间相差 10 分钟")
    a["srv"].peer_service.ping_all()
    assert A("/api/team")["peers"][0]["error"] is None  # 时间对了就好了


def test_unreachable_says_what_to_check(home):
    with pytest.raises(peer.PeerError) as ei:  # 端口上没有程序：被拒绝
        peer.call(f"127.0.0.1:{_free_port()}", "GET", "/peer/hello", timeout=5)
    assert "没有程序在这个端口上等着" in str(ei.value)
    timeout = peer._unreachable("10.0.0.9:25675", urllib.error.URLError(TimeoutError("timed out")))
    assert timeout.startswith("连不上 10.0.0.9:25675") and "防火墙" in timeout


def test_peer_port_rejects_unsigned_and_wrong_content_type(two_nodes):
    a, b = two_nodes
    url = f"http://{b['peer']}/peer/message"
    body = json.dumps({"uid": "x:1", "text": "hi"}).encode("utf-8")
    for ctype, expect in (("application/json", 403), ("text/plain", 415)):
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == expect
    # 团队端口上没有本机网页的接口
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(f"http://{b['peer']}/api/state", timeout=10)
    assert ei.value.code == 404
    hello = json.loads(urllib.request.urlopen(f"http://{b['peer']}/peer/hello", timeout=10).read())
    assert hello["app"] == "kopitiam" and "secret" not in hello
