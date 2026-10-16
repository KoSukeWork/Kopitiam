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
        raise SystemExit(f"错误：端口 {port} 被占用（{e}）。换一个端口：kopitiam web --port 8766")
    return srv, token, f"http://127.0.0.1:{port}/?t={token}"


def run(port: int = 8765, open_browser: bool = True) -> None:
    srv, token, url = make_server(port)
    print(f"kopitiam web 已启动：{url}")  # 网页进程不是 MCP，可以往 stdout 打印
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


class _Handler(BaseHTTPRequestHandler):
    server_version = "kopitiam-web"
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
<title>kopitiam</title>
<style>
:root {
  color-scheme: light dark;
  --bg:#f3f4f6; --panel:#ffffff; --hover:#f2f3f5; --ink:#1d2129; --ink-2:#4e5969; --muted:#86909c; --line:#e5e6eb;
  --accent:#3370ff; --accent-soft:rgba(51,112,255,.10); --ok:#00b42a; --name-l:40%; --shadow:0 1px 2px rgba(15,23,42,.06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0f1115; --panel:#171a1f; --hover:#1f232a; --ink:#e6e8eb; --ink-2:#b4bac3; --muted:#7d8590; --line:#272b32;
    --accent:#5b8dff; --accent-soft:rgba(91,141,255,.16); --ok:#3ccf62; --name-l:70%; --shadow:none;
  }
}
* { box-sizing:border-box; }
[hidden] { display:none !important; }
html, body { height:100%; }
body {
  margin:0; display:flex; background:var(--bg); color:var(--ink);
  font:14px/1.5 "Segoe UI", system-ui, -apple-system, "Microsoft YaHei UI", "PingFang SC", sans-serif;
  -webkit-font-smoothing:antialiased;
}
button, input, textarea { font:inherit; color:inherit; }
::placeholder { color:var(--muted); }
::-webkit-scrollbar { width:10px; height:10px; }
::-webkit-scrollbar-thumb { background:var(--line); border-radius:10px; border:3px solid transparent; background-clip:content-box; }

/* ---- 左栏 ---- */
aside { width:280px; flex-shrink:0; display:flex; flex-direction:column; background:var(--panel); border-right:1px solid var(--line); }
.brand { padding:18px 20px 10px; }
.brand .logo { font-size:17px; font-weight:700; letter-spacing:.2px; }
.brand .sub { font-size:12px; color:var(--muted); margin-top:2px; }
#agents { flex:1; overflow-y:auto; padding:2px 10px 16px; }
.section { display:flex; align-items:center; gap:6px; padding:14px 10px 6px; font-size:12px; font-weight:600; color:var(--muted); letter-spacing:.3px; }
details.offline > summary { list-style:none; cursor:pointer; user-select:none; }
details.offline > summary::-webkit-details-marker { display:none; }
details.offline > summary::before { content:"▸"; display:inline-block; font-size:10px; transition:transform .15s; }
details.offline[open] > summary::before { transform:rotate(90deg); }
details.offline > summary:hover { color:var(--ink-2); }

.agent { display:flex; align-items:center; gap:11px; padding:8px 10px; margin:2px 0; border-radius:10px; cursor:pointer; }
.agent:hover { background:var(--hover); }
.agent.active { background:var(--accent-soft); }
.agent.active .name { color:var(--accent); }
.avatar {
  position:relative; flex-shrink:0; width:34px; height:34px; border-radius:50%;
  display:grid; place-items:center; font-size:14px; font-weight:600; color:#fff;
  background:hsl(var(--h) 55% 52%);
}
.avatar .st { position:absolute; right:-1px; bottom:-1px; width:11px; height:11px; border-radius:50%; background:var(--ok); border:2px solid var(--panel); }
.agent.off .avatar { filter:grayscale(1); opacity:.5; }
.agent .info { flex:1; min-width:0; }
.agent .line1 { display:flex; align-items:center; gap:6px; min-width:0; }
.agent .name { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.kind { flex-shrink:0; padding:0 6px; border-radius:4px; font-size:11px; line-height:17px; color:var(--ink-2); background:var(--hover); }
.agent:hover .kind, .agent.active .kind { background:var(--panel); }
.agent .cwd { margin-top:1px; font-size:12px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.agent .add {
  flex-shrink:0; width:26px; height:26px; display:grid; place-items:center; cursor:pointer;
  border:1px solid var(--line); border-radius:7px; background:var(--panel); color:var(--ink-2); font-size:15px; line-height:1;
}
.agent .add:hover { color:var(--accent); border-color:var(--accent); }
@media (hover:hover) {
  .agent .add { opacity:0; }
  .agent:hover .add, .agent .add:focus-visible { opacity:1; }
}

/* ---- 主区 ---- */
main { flex:1; min-width:0; display:flex; flex-direction:column; }
#convbar { flex-shrink:0; display:flex; align-items:center; gap:12px; height:58px; padding:0 24px; background:var(--panel); border-bottom:1px solid var(--line); }
#convbar .titles { flex:1; min-width:0; }
#convtitle { font-size:15px; font-weight:600; }
#convsub { font-size:12px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.btn { padding:5px 12px; border:1px solid var(--line); border-radius:8px; background:var(--panel); color:var(--ink); font-size:13px; cursor:pointer; }
.btn:hover { background:var(--hover); }
#menuBtn { display:none; }

#stream { flex:1; overflow-y:auto; padding:22px 24px 8px; }
#empty { margin-top:18vh; text-align:center; font-size:13px; color:var(--muted); }
.msg { display:flex; align-items:flex-start; gap:10px; margin:0 0 18px; }
.msg .avatar { width:32px; height:32px; font-size:13px; }
.msg .body { min-width:0; max-width:min(680px, 76%); display:flex; flex-direction:column; align-items:flex-start; }
.meta { display:flex; align-items:baseline; flex-wrap:wrap; gap:6px; margin-bottom:4px; font-size:12px; color:var(--muted); }
.meta .who { font-size:13px; font-weight:600; color:hsl(var(--h) 60% var(--name-l)); }
.meta .id { opacity:.7; }
.bubble {
  padding:9px 13px; border:1px solid var(--line); border-radius:4px 14px 14px 14px;
  background:var(--panel); box-shadow:var(--shadow); line-height:1.6; white-space:pre-wrap; word-break:break-word;
}
.msg.mine { flex-direction:row-reverse; }
.msg.mine .body { align-items:flex-end; }
.msg.mine .meta { justify-content:flex-end; }
.msg.mine .bubble { background:var(--accent); color:#fff; border-color:transparent; border-radius:14px 4px 14px 14px; }
.notice { margin:2px 0 18px; text-align:center; }
.notice span { padding:3px 12px; border-radius:999px; font-size:12px; color:var(--muted); background:var(--hover); }
.files { display:flex; flex-direction:column; gap:6px; margin-top:6px; max-width:100%; }
.file { display:flex; align-items:center; gap:10px; min-width:240px; max-width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:10px; background:var(--panel); }
.file .ic { font-size:18px; }
.file .fn { flex:1; min-width:0; }
.file a { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--ink); font-weight:500; text-decoration:none; }
.file a:hover { color:var(--accent); text-decoration:underline; }
.file .sz { font-size:11.5px; color:var(--muted); }
.file .btn { flex-shrink:0; padding:3px 8px; font-size:12px; }

/* ---- 输入区 ---- */
#composer { flex-shrink:0; padding:10px 24px 18px; }
.box { border:1px solid var(--line); border-radius:14px; background:var(--panel); box-shadow:var(--shadow); transition:border-color .15s, box-shadow .15s; }
.box:focus-within { border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
.torow { display:flex; align-items:center; gap:10px; padding:8px 14px; border-bottom:1px solid var(--line); }
.torow .lbl { flex-shrink:0; font-size:12px; color:var(--muted); }
#to { flex:1; min-width:0; padding:2px 0; border:0; outline:0; background:transparent; }
#text { display:block; width:100%; min-height:78px; max-height:40vh; padding:10px 14px; border:0; outline:0; resize:vertical; background:transparent; line-height:1.55; }
.tools { display:flex; align-items:center; flex-wrap:wrap; gap:8px; padding:4px 10px 10px; }
.spacer { flex:1; }
.hint { font-size:12px; color:var(--muted); }
.primary { padding:6px 18px; border:0; border-radius:8px; background:var(--accent); color:#fff; font-weight:600; cursor:pointer; }
.primary:hover { filter:brightness(1.08); }
#chiprow { display:flex; flex-wrap:wrap; gap:6px; }
.chip { display:inline-flex; align-items:center; gap:2px; padding:2px 4px 2px 10px; border:1px solid var(--line); border-radius:999px; background:var(--hover); font-size:12px; }
.chip button { padding:0 4px; border:0; background:transparent; color:var(--muted); font-size:13px; cursor:pointer; }
.chip button:hover { color:var(--ink); }

#backdrop { display:none; }
@media (max-width: 720px) {
  aside { position:fixed; top:0; bottom:0; left:0; z-index:20; width:min(300px, 86vw); transform:translateX(-100%); transition:transform .2s; }
  aside.open { transform:none; box-shadow:4px 0 24px rgba(0,0,0,.25); }
  #backdrop.show { display:block; position:fixed; inset:0; z-index:10; background:rgba(0,0,0,.35); }
  #menuBtn { display:inline-block; }
  #convbar { padding:0 12px; }
  #stream { padding:16px 12px 8px; }
  #composer { padding:8px 12px 12px; }
  .msg .body { max-width:85%; }
  .file { min-width:0; }
  .hint { display:none; }
}
</style>
</head>
<body>
<aside id="side">
  <div class="brand"><div class="logo">☕ kopitiam</div><div class="sub" id="brandsub">本地聊天室</div></div>
  <div id="agents"></div>
</aside>
<div id="backdrop"></div>
<main>
  <div id="convbar">
    <button id="menuBtn" class="btn" aria-label="成员列表">☰</button>
    <div class="titles"><div id="convtitle">全部消息</div><div id="convsub">所有人之间的消息都在这里</div></div>
    <button id="showAll" class="btn" hidden>显示全部</button>
  </div>
  <div id="stream"><div id="list"></div><div id="empty">还没有消息</div></div>
  <div id="composer">
    <div class="box">
      <div class="torow"><span class="lbl">发给</span><input id="to" placeholder="所有人（多个名字用逗号分隔）"></div>
      <textarea id="text" placeholder="写点什么…"></textarea>
      <div class="tools">
        <input id="file" type="file" multiple hidden>
        <button id="pick" class="btn">📎 附件</button>
        <span id="chiprow"></span>
        <span class="spacer"></span>
        <span class="hint">Ctrl+Enter 发送</span>
        <button id="send" class="primary">发送</button>
      </div>
    </div>
  </div>
</main>
<script>
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);
let lastId = 0, pendingFiles = [], agentJson = "", offlineOpen = false;
let selected = null;  // 当前对话的 agent 名字；null = 显示全部消息
let agentsByName = {};
const known = new Set();

function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ "X-Token": TOKEN }, opts.headers || {});
  return fetch(path, opts).then(r => r.json().catch(() => ({})));
}

// 只用 textContent 写入文字：消息和名字都是 agent 写的，绝不用 innerHTML
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function hue(name) {
  let h = 0;
  for (const c of name) h = (h * 31 + c.codePointAt(0)) % 360;
  return h;
}

function shown(name) { return name === "user" ? "你" : name; }

function avatar(name, online) {
  const a = el("div", "avatar", name === "user" ? "你" : [...name][0].toUpperCase());
  a.style.setProperty("--h", hue(name));
  if (online) a.append(el("span", "st"));
  return a;
}

function fmtTime(iso) {
  const d = new Date(iso), now = new Date();
  const hm = d.toTimeString().slice(0, 5);
  if (d.toDateString() === now.toDateString()) return hm;
  return String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0") + " " + hm;
}

function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

function renderAgents(agents) {
  agentsByName = Object.fromEntries(agents.map(a => [a.name, a]));
  const j = JSON.stringify(agents) + "|" + selected;
  if (j === agentJson) return;
  agentJson = j;
  const online = agents.filter(a => a.online), offline = agents.filter(a => !a.online);
  const n = online.filter(a => a.name !== "user").length;
  $("brandsub").textContent = n ? n + " 个 agent 在线" : "本地聊天室 · 暂无 agent 在线";
  const box = $("agents");
  box.textContent = "";
  box.append(el("div", "section", "在线 · " + online.length));
  for (const a of online) box.append(agentRow(a));
  if (offline.length) {
    // 离线的默认折叠；轮询重绘时保持用户上次的展开状态
    const det = el("details", "offline");
    det.open = offlineOpen;
    det.ontoggle = () => { offlineOpen = det.open; };
    det.append(el("summary", "section", "离线 · " + offline.length));
    for (const a of offline) det.append(agentRow(a));
    box.append(det);
  }
  if (selected) updateConvbar();
}

function agentRow(a) {
  const div = el("div", "agent" + (a.online ? "" : " off") + (a.name === selected ? " active" : ""));
  div.title = a.cwd;
  const info = el("div", "info");
  const line1 = el("div", "line1");
  line1.append(el("span", "name", a.name));
  if (a.name !== "user") line1.append(el("span", "kind", a.kind));
  info.append(line1, el("div", "cwd", a.name === "user" ? "你 · 网页" : a.cwd));
  div.append(avatar(a.name, a.online), info);
  if (a.name !== "user") {
    const add = el("button", "add", "＋");
    add.title = "加到「发给」（可以同时发给多个）";
    add.onclick = (e) => {
      e.stopPropagation();  // 只加收件人，不切换对话
      const cur = $("to").value.split(",").map(s => s.trim()).filter(Boolean);
      if (!cur.includes(a.name)) cur.push(a.name);
      $("to").value = cur.join(", ");
    };
    div.append(add);
  }
  // 点 agent = 和它对话；再点一次（或点 user）回到全部消息
  div.onclick = () => select(a.name === "user" || a.name === selected ? null : a.name);
  return div;
}

function involves(m, name) {
  // 和它有关：它发的、发给它的、以及广播（它也收得到）
  return m.sender === name || m.to.includes(name) || m.to.length === 0;
}

function updateEmpty() {
  const any = [...$("list").children].some(e => !e.hidden);
  $("empty").hidden = any;
  $("empty").textContent = selected ? "和 " + selected + " 还没有消息" : "还没有消息";
}

function applyFilter() {
  for (const e of $("list").children) e.hidden = selected !== null && !involves(e._m, selected);
  updateEmpty();
}

function updateConvbar() {
  const a = selected && agentsByName[selected];
  $("convtitle").textContent = selected ? "与 " + selected + " 的对话" : "全部消息";
  $("convsub").textContent = selected
    ? (a ? a.kind + " · " + (a.online ? "在线" : "离线") + " · " + a.cwd : "")
    : "所有人之间的消息都在这里";
  $("showAll").hidden = !selected;
}

function closeSide() { $("side").classList.remove("open"); $("backdrop").classList.remove("show"); }

function select(name) {
  selected = name;
  updateConvbar();
  $("to").value = name || "";
  agentJson = "";  // 强制重绘左栏，更新高亮
  poll();
  applyFilter();
  closeSide();
  const s = $("stream"); s.scrollTop = s.scrollHeight;
  $("text").focus();
}

function addMessage(m) {
  if (known.has(m.id)) return;
  known.add(m.id);
  if (m.id > lastId) lastId = m.id;  // 下次轮询只要更新的消息
  const mine = m.sender === "user";
  let div;
  if (/^（.+ 改名为 .+）$/.test(m.text) && !m.files.length) {
    div = el("div", "notice");  // 改名通知：居中灰色小字
    div.append(el("span", "", m.text.slice(1, -1)));
  } else {
    div = el("div", "msg" + (mine ? " mine" : ""));
    if (!mine) div.append(avatar(m.sender, false));
    const body = el("div", "body");
    const meta = el("div", "meta");
    const who = el("span", "who", shown(m.sender));
    who.style.setProperty("--h", hue(m.sender));
    meta.append(who, el("span", "", "→ " + (m.to.length ? m.to.map(shown).join("、") : "所有人")),
                el("span", "", fmtTime(m.time)), el("span", "id", "#" + m.id));
    body.append(meta);
    if (m.text) body.append(el("div", "bubble", m.text));
    if ((m.files || []).length) {
      const files = el("div", "files");
      for (const f of m.files) {
        const card = el("div", "file");
        const fn = el("div", "fn");
        const a = el("a", "", f.name);
        a.href = f.url; a.target = "_blank"; a.title = f.path;
        fn.append(a, el("div", "sz", fmtSize(f.size)));
        const btn = el("button", "btn", "复制路径");
        btn.onclick = () => navigator.clipboard.writeText(f.path).then(() => {
          btn.textContent = "已复制"; setTimeout(() => { btn.textContent = "复制路径"; }, 1200);
        });
        card.append(el("span", "ic", "📄"), fn, btn);
        files.append(card);
      }
      body.append(files);
    }
    div.append(body);
  }
  div._m = m;
  div.hidden = selected !== null && !involves(m, selected);
  $("list").append(div);
}

async function poll() {
  let st;
  try { st = await api("/api/state?after=" + lastId); } catch (e) { return; }
  const s = $("stream");
  const atBottom = s.scrollTop + s.clientHeight >= s.scrollHeight - 40;
  for (const m of (st.messages || [])) addMessage(m);
  renderAgents(st.agents || []);
  updateEmpty();
  if ((st.messages || []).length && atBottom) s.scrollTop = s.scrollHeight;
}

function renderChips() {
  const row = $("chiprow");
  row.textContent = "";
  pendingFiles.forEach((f, i) => {
    const chip = el("span", "chip", "📎 " + f.name);
    chip.title = f.path;
    const x = el("button", "", "✕");
    x.title = "移除";
    x.onclick = () => { pendingFiles.splice(i, 1); renderChips(); };
    chip.append(x);
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
$("showAll").onclick = () => select(null);
$("text").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.ctrlKey) send(); });
$("pick").onclick = () => $("file").click();
$("file").onchange = (e) => { upload([...e.target.files]); e.target.value = ""; };
$("menuBtn").onclick = () => { $("side").classList.add("open"); $("backdrop").classList.add("show"); };
$("backdrop").onclick = closeSide;
poll();
setInterval(poll, 1500);
</script>
</body>
</html>
"""
