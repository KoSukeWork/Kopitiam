"""团队（跨电脑）：和别人电脑上的 kopitiam 互发消息。

只在人和人之间：远端发来的消息只给本机 user，agent 看不到；agent 也不能直接发给远端，
要由用户在网页上转发。这样别人没法绕过你，直接给你的 agent 下指令。

安全：
- 团队端口只开 /peer/* 这几个接口；本机网页的 /api、/files 不在这里，仍然只给 127.0.0.1。
- 配对：对方网页上生成 6 位配对码（10 分钟有效、最多试 5 次），配对成功后双方保存一个共享密钥。
- 之后每个请求都带 HMAC-SHA256 签名（方法、路径、时间戳、body 哈希），时间戳差 5 分钟以上拒绝。
- 只用标准库，内容不加密：同一个局域网直接用；跨网络请用 Tailscale 这类自带加密的组网工具。
- JSON 接口要求 Content-Type: application/json，文件接口要求 application/octet-stream：
  浏览器里别的网站发这种跨站请求会先触发 CORS 预检，发不出来。
- 访问别人时不走系统代理（局域网地址不该走代理，也免得被抓包工具截下）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import db
from .config import (
    FILE_MAX,
    FILES_MAX,
    FORWARD_EXTRA,
    PAIR_CODE_TTL,
    PAIR_MAX_TRIES,
    PEER_BODY_MAX,
    PEER_CLOCK_SKEW,
    PEER_PORT,
    TEXT_MAX,
    staging_dir,
)

PROTO = 1
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 不走系统代理


class PeerError(Exception):
    """联系对方失败；str(e) 是给用户看的原因。"""


# ---- 签名 ----

def sign(secret: str, method: str, path: str, ts: str, body: bytes) -> str:
    msg = "\n".join([method, path, ts, hashlib.sha256(body).hexdigest()]).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify(secret: str, method: str, path: str, ts: str | None, body: bytes, sig: str | None) -> bool:
    try:
        t = int(ts or "")
    except ValueError:
        return False
    if abs(time.time() - t) > PEER_CLOCK_SKEW:
        return False
    return hmac.compare_digest(sign(secret, method, path, str(t), body), sig or "")


# ---- 地址 ----

def normalize_address(addr: str) -> str:
    """'192.168.1.5' → '192.168.1.5:8766'；去掉 http:// 和路径。暂不支持 IPv6。"""
    a = (addr or "").strip()
    for prefix in ("http://", "https://"):
        if a.lower().startswith(prefix):
            a = a[len(prefix):]
    a = a.split("/")[0].strip()
    if not a:
        raise PeerError("地址不能为空")
    if ":" in a:
        host, _, port = a.rpartition(":")
        if not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise PeerError(f"地址格式不对：{addr}（例如 192.168.1.5 或 192.168.1.5:{PEER_PORT}）")
        return f"{host}:{int(port)}"
    return f"{a}:{PEER_PORT}"


def local_addresses() -> list[str]:
    """本机的局域网 IPv4 地址（给「让别人加我」显示用）。"""
    ips: list[str] = []
    try:  # UDP connect 不会真的发包，只是让系统选出默认出口网卡的地址
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ips += [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    out: list[str] = []
    for ip in ips:
        if ip not in out and not ip.startswith("127."):
            out.append(ip)
    return out


# ---- 访问别人 ----

def call(address: str, method: str, path: str, payload: dict | None = None, *, raw: bytes | None = None,
         secret: str | None = None, my_port: int = 0, timeout: float = 15) -> dict:
    if raw is not None:
        body, ctype = raw, "application/octet-stream"
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        ctype = "application/json"
    headers = {"Content-Type": ctype}
    if secret:
        ts = str(int(time.time()))
        headers |= {
            "X-Kopitiam-Node": db.node_id(),
            "X-Kopitiam-Time": ts,
            "X-Kopitiam-Sig": sign(secret, method, path, ts, body),
            "X-Kopitiam-Port": str(my_port),  # 我的团队端口（0 = 没开）：对方据此更新我的地址
        }
    req = urllib.request.Request(f"http://{address}{path}", data=body if method == "POST" else None,
                                 headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error")
        except ValueError:
            msg = None
        raise PeerError(msg or f"对方拒绝了请求（HTTP {e.code}）") from None
    except (urllib.error.URLError, OSError) as e:
        raise PeerError(f"连不上 {address}（{getattr(e, 'reason', e)}）") from None
    try:
        return json.loads(data or b"{}")
    except ValueError:
        raise PeerError("对方返回的不是 kopitiam 的数据") from None


def _staged(peer_node: str, uid: str, i: int) -> Path:
    key = hashlib.sha256(f"{peer_node}|{uid}".encode("utf-8")).hexdigest()[:24]
    return staging_dir() / f"{key}_{i}"


# ---- 服务：团队端口（收）+ 后台线程（投递、定时 ping） ----

class PeerService:
    """在网页进程里运行。团队端口默认关闭，在网页上开启；开启状态记在数据库里，下次启动自动打开。"""

    def __init__(self, host: str = "0.0.0.0", port: int = PEER_PORT):
        self.host, self.port = host, port
        self.server: ThreadingHTTPServer | None = None
        self._code: dict | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None

    @property
    def sharing(self) -> bool:
        return self.server is not None

    @property
    def bound_port(self) -> int:
        return self.server.server_address[1] if self.server else 0

    def start_sharing(self) -> None:
        if self.server:
            return
        handler = type("PeerHandler", (_PeerHandler,), {"service": self})
        try:
            self.server = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as e:
            raise PeerError(f"团队端口 {self.host}:{self.port} 打不开（{e}）") from None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        db.set_meta("share", "1")

    def stop_sharing(self) -> None:
        self._close_server()
        db.set_meta("share", "0")

    def _close_server(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        with self._lock:
            self._code = None

    def start_worker(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._loop, daemon=True)
            self._worker.start()

    def stop(self) -> None:
        """进程退出：停后台线程、关端口（不改「下次启动是否开启」的设置）。"""
        self._stop.set()
        self._close_server()

    # -- 配对码（别人来加我） --

    def new_code(self) -> tuple[str, int]:
        if not self.sharing:
            raise PeerError("要先开启团队连接，别人才能连进来")
        code = f"{secrets.randbelow(10 ** 6):06d}"
        with self._lock:
            self._code = {"code": code, "expires": time.time() + PAIR_CODE_TTL, "tries": 0}
        return code, PAIR_CODE_TTL

    def code_status(self) -> dict | None:
        with self._lock:
            c = self._code
            if c and time.time() < c["expires"]:
                return {"code": c["code"], "expires_in": int(c["expires"] - time.time())}
        return None

    def check_code(self, code: str) -> bool:
        """一次性：对了就作废；错了计数，错满 5 次也作废（防止被挨个试）。"""
        with self._lock:
            c = self._code
            if not c or time.time() > c["expires"]:
                self._code = None
                return False
            if hmac.compare_digest(c["code"], code):
                self._code = None
                return True
            c["tries"] += 1
            if c["tries"] >= PAIR_MAX_TRIES:
                self._code = None
            return False

    # -- 我去加别人 / 解除配对 --

    def join(self, address: str, code: str) -> str:
        address = normalize_address(address)
        code = (code or "").strip()
        if not (len(code) == 6 and code.isdigit()):
            raise PeerError("配对码是 6 位数字")
        hello = call(address, "GET", "/peer/hello", timeout=8)
        if hello.get("app") != "kopitiam":
            raise PeerError(f"{address} 上不是 kopitiam")
        if hello.get("node_id") == db.node_id():
            raise PeerError("这是你自己这台电脑")
        resp = call(address, "POST", "/peer/pair",
                    {"code": code, "node_id": db.node_id(), "name": db.display_name(), "port": self.bound_port})
        secret, node = resp.get("secret"), resp.get("node_id")
        if not secret or not node:
            raise PeerError("配对失败：对方没有返回密钥")
        return db.add_peer(node, str(resp.get("name") or hello.get("name") or "peer")[:32], address, secret)["alias"]

    def unpair(self, alias: str) -> None:
        peer = db.peer_by_alias(alias)
        if peer is None:
            raise PeerError(f"找不到 {alias}")
        try:  # 尽量通知对方；对方不在线也照样在本机解除
            call(peer["address"], "POST", "/peer/unpair", {}, secret=peer["secret"], my_port=self.bound_port, timeout=5)
        except PeerError:
            pass
        db.remove_peer(peer["id"])

    # -- 后台：投递发件箱、定时 ping、清理临时文件 --

    def _loop(self) -> None:
        last_ping = last_clean = 0.0
        while not self._stop.wait(2):
            try:
                self.deliver_due()
                if time.time() - last_ping > 20:
                    self.ping_all()
                    last_ping = time.time()
                if time.time() - last_clean > 600:
                    _clean_staging()
                    last_clean = time.time()
            except Exception:  # NOTE: 后台线程不能因为一次意外就退出，下一轮再试
                pass

    def ping_all(self) -> None:
        for p in db.list_peers():
            try:
                r = call(p["address"], "POST", "/peer/ping", {}, secret=p["secret"], my_port=self.bound_port, timeout=5)
                db.peer_seen(p["id"], name=str(r.get("name") or "")[:32] or None)
            except PeerError:
                pass

    def deliver_due(self) -> None:
        me = db.node_id()
        for item in db.due_outbox():
            uid = f"{me}:{item['message_id']}"
            try:
                metas = []
                for i, f in enumerate(item["files"]):
                    data = Path(f["path"]).read_bytes()  # 单个 ≤ 20 MB
                    call(item["address"], "POST", f"/peer/file?uid={quote(uid)}&i={i}", raw=data,
                         secret=item["secret"], my_port=self.bound_port, timeout=120)
                    metas.append({"i": i, "name": f["orig_name"], "size": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()})
                call(item["address"], "POST", "/peer/message", {"uid": uid, "text": item["text"], "files": metas},
                     secret=item["secret"], my_port=self.bound_port, timeout=30)
                db.outbox_done(item["ob_id"])
                db.peer_seen(item["peer_id"])
            except (PeerError, OSError) as e:
                db.outbox_retry(item["ob_id"], item["tries"], str(e))


def _clean_staging(max_age: float = 24 * 3600) -> None:
    """收了一半没收完的附件（对方中途断线）放久了就删。"""
    d = staging_dir()
    if not d.is_dir():
        return
    for p in d.iterdir():
        try:
            if time.time() - p.stat().st_mtime > max_age:
                p.unlink()
        except OSError:
            pass


# ---- 团队端口上的请求处理 ----

class _PeerHandler(BaseHTTPRequestHandler):
    service: PeerService
    server_version = "kopitiam-peer"

    def log_message(self, fmt, *args) -> None:
        pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def _ctype(self) -> str:
        return (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()

    def _body(self, limit: int) -> bytes | None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > limit:
            self._err(413, "请求体太大")
            return None
        return self.rfile.read(n)

    def _auth(self, body: bytes):
        peer = db.peer_by_node(self.headers.get("X-Kopitiam-Node", ""))
        if peer is None or not verify(peer["secret"], self.command, self.path,
                                      self.headers.get("X-Kopitiam-Time"), body, self.headers.get("X-Kopitiam-Sig")):
            self._err(403, "没有配对，或者签名不对")
            return None
        # 签名通过：记下对方现在的地址（对方 IP 变了也能自动跟上；端口 0 表示对方没开团队连接）
        port = self.headers.get("X-Kopitiam-Port", "0")
        addr = f"{self.client_address[0]}:{int(port)}" if port.isdigit() and 0 < int(port) < 65536 else None
        db.peer_seen(peer["id"], address=addr)
        return peer

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/peer/hello":
            return self._json({"app": "kopitiam", "proto": PROTO, "node_id": db.node_id(), "name": db.display_name()})
        self._err(404, "not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/peer/file":
            return self._file()
        if self._ctype() != "application/json":
            return self._err(415, "需要 application/json")
        body = self._body(PEER_BODY_MAX)
        if body is None:
            return
        if path == "/peer/pair":
            return self._pair(body)
        peer = self._auth(body)
        if peer is None:
            return
        if path == "/peer/ping":
            return self._json({"ok": True, "name": db.display_name()})
        if path == "/peer/message":
            return self._message(peer, body)
        if path == "/peer/unpair":
            db.remove_peer(peer["id"])
            return self._json({"ok": True})
        self._err(404, "not found")

    def _pair(self, body: bytes) -> None:
        try:
            data = json.loads(body)
        except ValueError:
            return self._err(400, "bad json")
        if not self.service.check_code(str(data.get("code") or "")):
            return self._err(403, "配对码不对或已过期，请让对方重新生成一个")
        node = str(data.get("node_id") or "")
        if not node or len(node) > 64 or node == db.node_id():
            return self._err(400, "bad node_id")
        port = data.get("port")
        port = port if isinstance(port, int) and 0 < port < 65536 else 0
        secret = secrets.token_hex(32)
        db.add_peer(node, str(data.get("name") or "peer")[:32], f"{self.client_address[0]}:{port}", secret)
        self._json({"node_id": db.node_id(), "name": db.display_name(), "secret": secret})

    def _file(self) -> None:
        if self._ctype() != "application/octet-stream":
            return self._err(415, "需要 application/octet-stream")
        body = self._body(FILE_MAX)
        if body is None:
            return
        peer = self._auth(body)
        if peer is None:
            return
        q = parse_qs(urlparse(self.path).query)
        uid, i = q.get("uid", [""])[0], q.get("i", [""])[0]
        if not uid.startswith(peer["node_id"] + ":") or len(uid) > 200 or not i.isdigit() or int(i) >= FILES_MAX:
            return self._err(400, "bad file")
        p = _staged(peer["node_id"], uid, int(i))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        self._json({"ok": True})

    def _message(self, peer, body: bytes) -> None:
        try:
            data = json.loads(body)
        except ValueError:
            return self._err(400, "bad json")
        uid, text, files = str(data.get("uid") or ""), str(data.get("text") or ""), data.get("files") or []
        # uid 必须是对方自己的消息 id，不能冒充别人
        if not uid.startswith(peer["node_id"] + ":") or len(uid) > 200:
            return self._err(400, "bad uid")
        if len(text) > TEXT_MAX + FORWARD_EXTRA or not isinstance(files, list) or len(files) > FILES_MAX:
            return self._err(400, "消息太长或附件太多")
        staged: list[tuple[Path, str]] = []
        for f in files:
            i = f.get("i") if isinstance(f, dict) else None
            if not isinstance(i, int) or not 0 <= i < FILES_MAX:
                return self._err(400, "bad file index")
            p = _staged(peer["node_id"], uid, i)
            if not p.is_file() or p.stat().st_size != f.get("size") or db.file_sha256(p) != f.get("sha256"):
                return self._err(400, "附件不完整，请重发")
            staged.append((p, str(f.get("name") or "file")[:200]))
        if not text.strip() and not staged:
            return self._err(400, "空消息")
        db.receive_remote(peer["node_id"], uid, text, staged)
        self._json({"ok": True})
