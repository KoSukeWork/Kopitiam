"""本地网页（§8）：标准库 http.server，只绑定 127.0.0.1，网页以 user 身份发言。

安全：每个请求都查 Host；/api/* 要 X-Token；/files/* 接受 URL 参数 t；路径逃逸一律 404；
不返回任何 Access-Control-Allow-* 头；消息渲染只用 textContent。
"""
from __future__ import annotations

import json
import secrets
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import db
from .config import FILE_MAX, files_dir, safe
from .db import Chat

TEXT_SUFFIXES = {".md", ".txt", ".json", ".log"}
SEND_BODY_MAX = 1024 * 1024


def make_server(port: int = 8765) -> tuple[ThreadingHTTPServer, str, str]:
    """起 HTTP 服务，返回 (server, token, url)。port=0 让系统随机分配（测试用）。"""
    db.init_db()
    token = secrets.token_urlsafe(16)
    user = db.user_id()

    class Handler(_Handler):
        pass

    Handler.token = token
    Handler.user_id_value = user
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise SystemExit(f"错误：端口 {port} 被占用（{e}）。换一个端口：agent-chat web --port 8766")
    return srv, token, f"http://127.0.0.1:{port}/?t={token}"


def run(port: int = 8765, open_browser: bool = True) -> None:
    srv, token, url = make_server(port)
    print(f"agent-chat web 已启动：{url}")  # 网页进程不是 MCP，可以往 stdout 打印
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


class _Handler(BaseHTTPRequestHandler):
    server_version = "agent-chat-web"
    token: str = ""
    user_id_value: int = 0

    def log_message(self, fmt, *args) -> None:  # 访问日志别刷屏
        pass

    # ---- 基础 ----

    def _port(self) -> int:
        return self.server.server_address[1]

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{self._port()}", f"localhost:{self._port()}")

    def _token_header_ok(self) -> bool:
        return self.headers.get("X-Token", "") == self.token

    def _send_bytes(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8", code)

    def _deny(self, code: int = 403) -> None:
        self._json({"error": "forbidden"}, code)

    # ---- 路由 ----

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._deny()
        u = urlparse(self.path)
        if u.path == "/":
            return self._send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/api/state":
            if not self._token_header_ok():
                return self._deny()
            try:
                after = int(parse_qs(u.query).get("after", ["0"])[0] or 0)
            except ValueError:
                return self._json({"error": "bad after"}, 400)
            chat = Chat()
            msgs = chat.messages_after(after, 200)
            for m in msgs:
                for f in m["files"]:
                    f["url"] = f"/files/{quote(Path(f['path']).name)}?t={self.token}"
            return self._json({"agents": chat.agents_brief(), "messages": msgs})
        if u.path.startswith("/files/"):
            if parse_qs(u.query).get("t", [""])[0] != self.token:
                return self._deny()
            return self._serve_file(unquote(u.path[len("/files/"):]))
        return self._deny(404)

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._deny()
        if not self._token_header_ok():
            return self._deny()
        u = urlparse(self.path)
        if u.path == "/api/send":
            return self._api_send()
        if u.path == "/api/upload":
            return self._api_upload(parse_qs(u.query))
        return self._deny(404)

    # ---- 接口 ----

    def _api_send(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > SEND_BODY_MAX:
            return self._json({"ok": False, "error": "请求体太大"}, 413)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._json({"ok": False, "error": "bad json"}, 400)
        text = str(data.get("text") or "")
        to = [str(t) for t in (data.get("to") or [])]
        files = [str(f) for f in (data.get("files") or [])]
        out = Chat().send(self.user_id_value, text, to, files)
        self._json({"ok": not out.startswith("错误："), "message": out})

    def _api_upload(self, qs) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > FILE_MAX:
            return self._json({"ok": False, "error": "文件超过 20 MB"}, 413)
        name = safe(unquote(qs.get("name", ["file"])[0]) or "file")
        dest = self._upload_path(name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.rfile.read(length))
        self._json({"ok": True, "path": str(dest)})

    def _upload_path(self, name: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        cand = files_dir() / f"upload_{stamp}_{name}"
        i = 1
        while cand.exists():
            cand = files_dir() / f"upload_{stamp}_{i}_{name}"
            i += 1
        return cand

    def _serve_file(self, name: str) -> None:
        root = files_dir().resolve()
        resolved = (files_dir() / name).resolve()
        # 只能访问 FILES_DIR 里面的文件
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return self._deny(404)
        if resolved.suffix.lower() in TEXT_SUFFIXES:
            ctype = "text/plain; charset=utf-8"
        else:
            ctype = "application/octet-stream"
        try:
            body = resolved.read_bytes()
        except OSError:
            return self._deny(404)
        self._send_bytes(body, ctype)


# ---- 页面（HTML/CSS/JS 全部内嵌，不用外部 CDN） ----

INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-chat</title>
<style>
:root { --bg:#f6f7f9; --panel:#fff; --ink:#1c1e21; --muted:#8a929e; --line:#e4e7ec; --accent:#3370ff; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#15171a; --panel:#1f2227; --ink:#e8eaed; --muted:#9aa0a6; --line:#2c3037; --accent:#5b8dff; }
}
* { box-sizing: border-box; }
body { margin:0; font-family: system-ui, "Microsoft YaHei", sans-serif; background:var(--bg); color:var(--ink); display:flex; height:100vh; }
aside { width:260px; min-width:220px; background:var(--panel); border-right:1px solid var(--line); overflow-y:auto; padding:12px; }
aside h1 { font-size:14px; margin:4px 0 10px; color:var(--muted); }
.agent { padding:8px 10px; border-radius:8px; cursor:pointer; }
.agent:hover { background:var(--bg); }
.agent .top { display:flex; align-items:baseline; }
.agent .name { font-weight:600; }
.agent .kind { color:var(--muted); font-size:11px; margin-left:8px; }
.agent .cwd { color:var(--muted); font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.dot { margin-right:6px; font-size:10px; }
main { flex:1; display:flex; flex-direction:column; min-width:0; }
#stream { flex:1; overflow-y:auto; padding:16px 20px; }
.msg { max-width:72%; margin:6px 0; }
.msg.mine { margin-left:auto; }
.meta { font-size:11px; color:var(--muted); margin-bottom:2px; }
.bubble { display:inline-block; background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:8px 12px; white-space:pre-wrap; word-break:break-word; }
.msg.mine .bubble { background:var(--accent); color:#fff; border-color:var(--accent); }
.fileline { font-size:12px; margin-top:4px; }
.fileline a { color:var(--accent); }
.fileline button { margin-left:6px; font-size:11px; border:1px solid var(--line); background:transparent; color:var(--muted); border-radius:6px; cursor:pointer; }
#composer { border-top:1px solid var(--line); background:var(--panel); padding:10px 12px; }
#to { width:100%; padding:6px 8px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--ink); margin-bottom:8px; }
#text { width:100%; height:64px; resize:vertical; padding:8px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--ink); font:inherit; }
.row { display:flex; gap:8px; align-items:center; margin-top:8px; flex-wrap:wrap; }
button.act { padding:6px 14px; border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }
button.ghost { padding:6px 10px; border:1px solid var(--line); background:transparent; color:var(--ink); border-radius:8px; cursor:pointer; }
.chip { display:inline-flex; align-items:center; background:var(--bg); border:1px solid var(--line); border-radius:999px; padding:2px 10px; font-size:12px; cursor:pointer; }
#menuBtn { display:none; }
@media (max-width: 720px) {
  aside { position:fixed; left:0; top:0; bottom:0; z-index:5; transform:translateX(-100%); transition:transform .2s; }
  aside.open { transform:none; }
  #menuBtn { display:inline-block; }
}
</style>
</head>
<body>
<aside id="side"><h1>agent-chat</h1><div id="agents"></div></aside>
<main>
  <div id="stream"></div>
  <div id="composer">
    <input id="to" placeholder="发给（逗号分隔，留空 = 所有人）">
    <textarea id="text" placeholder="Ctrl+Enter 发送"></textarea>
    <div class="row">
      <button id="menuBtn" class="ghost">☰</button>
      <input id="file" type="file" multiple hidden>
      <button id="pick" class="ghost">📎 附件</button>
      <span id="chiprow" class="row"></span>
      <span style="flex:1"></span>
      <button id="send" class="act">发送</button>
    </div>
  </div>
</main>
<script>
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);
let lastId = 0, pendingFiles = [], agentJson = "";
const known = new Set();

function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ "X-Token": TOKEN }, opts.headers || {});
  return fetch(path, opts).then(r => r.json().catch(() => ({})));
}

function colorOf(name) {
  let h = 0;
  for (const c of name) h = (h * 31 + c.codePointAt(0)) % 360;
  return "hsl(" + h + ", 55%, 45%)";
}

function fmtTime(iso) {
  const d = new Date(iso), now = new Date();
  const hm = d.toTimeString().slice(0, 5);
  if (d.toDateString() === now.toDateString()) return hm;
  return String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0") + " " + hm;
}

function renderAgents(agents) {
  const j = JSON.stringify(agents);
  if (j === agentJson) return;
  agentJson = j;
  const box = $("agents");
  box.textContent = "";
  for (const a of agents) {
    const div = document.createElement("div");
    div.className = "agent";
    div.title = a.cwd;
    const top = document.createElement("div"); top.className = "top";
    const dot = document.createElement("span"); dot.className = "dot";
    dot.textContent = a.online ? "●" : "○";
    dot.style.color = a.online ? "#22c55e" : "var(--muted)";
    const name = document.createElement("span"); name.className = "name"; name.textContent = a.name;
    const kind = document.createElement("span"); kind.className = "kind"; kind.textContent = a.kind;
    top.append(dot, name, kind);
    const cwd = document.createElement("div"); cwd.className = "cwd";
    cwd.textContent = a.name === "user" ? "—" : a.cwd;
    div.append(top, cwd);
    div.onclick = () => {  // 点名字加进"发给"框
      const cur = $("to").value.split(",").map(s => s.trim()).filter(Boolean);
      if (!cur.includes(a.name) && a.name !== "user") cur.push(a.name);
      $("to").value = cur.join(", ");
    };
    box.append(div);
  }
}

function addMessage(m) {
  if (known.has(m.id)) return;
  known.add(m.id);
  const div = document.createElement("div");
  div.className = "msg" + (m.sender === "user" ? " mine" : "");
  const meta = document.createElement("div"); meta.className = "meta";
  meta.textContent = "#" + m.id + " " + fmtTime(m.time) + " " + m.sender + " → " + (m.to.length ? m.to.join("、") : "所有人");
  meta.style.color = colorOf(m.sender);
  const bubble = document.createElement("div"); bubble.className = "bubble";
  bubble.textContent = m.text;  // 消息是 agent 写的，绝不用 innerHTML 插内容
  div.append(meta, bubble);
  for (const f of (m.files || [])) {
    const line = document.createElement("div"); line.className = "fileline";
    const a = document.createElement("a");
    a.href = f.url; a.textContent = "📎 " + f.name; a.target = "_blank";
    const btn = document.createElement("button");
    btn.textContent = "复制路径";
    btn.onclick = () => navigator.clipboard.writeText(f.path);
    line.append(a, btn);
    div.append(line);
  }
  $("stream").append(div);
}

async function poll() {
  let st;
  try { st = await api("/api/state?after=" + lastId); } catch (e) { return; }
  const el = $("stream");
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
  for (const m of (st.messages || [])) addMessage(m);
  renderAgents(st.agents || []);
  if ((st.messages || []).length && atBottom) el.scrollTop = el.scrollHeight;
}

function renderChips() {
  const row = $("chiprow");
  row.textContent = "";
  pendingFiles.forEach((f, i) => {
    const chip = document.createElement("span"); chip.className = "chip";
    chip.textContent = f.name + " ✕";
    chip.title = f.path;
    chip.onclick = () => { pendingFiles.splice(i, 1); renderChips(); };
    row.append(chip);
  });
}

async function send() {
  const text = $("text").value;
  const to = $("to").value.split(",").map(s => s.trim()).filter(Boolean);
  if (!text.trim() && !pendingFiles.length) return;
  const out = await api("/api/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: text, to: to, files: pendingFiles.map(f => f.path) }),
  });
  if (!out.ok) { alert(out.message || "发送失败"); return; }
  $("text").value = "";
  pendingFiles = []; renderChips();
  poll();
}

async function upload(files) {
  for (const f of files) {
    const r = await api("/api/upload?name=" + encodeURIComponent(f.name), {
      method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: f,
    });
    if (r.ok) pendingFiles.push({ name: f.name, path: r.path });
  }
  renderChips();
}

$("send").onclick = send;
$("text").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.ctrlKey) send(); });
$("pick").onclick = () => $("file").click();
$("file").onchange = (e) => { upload([...e.target.files]); e.target.value = ""; };
$("menuBtn").onclick = () => $("side").classList.toggle("open");
poll();
setInterval(poll, 1500);
</script>
</body>
</html>
"""
