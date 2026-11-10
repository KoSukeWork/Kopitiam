"""本地网页（§8）：标准库 http.server，只绑定 127.0.0.1，网页以 user 身份发言。

安全：每个请求都查 Host；/api/* 要 X-Token；/files/* 接受 URL 参数 t；路径逃逸一律 404；
不返回任何 Access-Control-Allow-* 头；消息渲染只用 textContent。
团队（跨电脑）走另一个端口，见 peer.py；这里的 /api 和 /files 永远只给本机。
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import backup, db, peer
from .config import FILE_MAX, PEER_PORT, WEB_PORT, files_dir, safe
from .db import Chat
from .wake import detector
from .wake import service as wake_service
from .wake.service import WakeService

TEXT_SUFFIXES = {".md", ".txt", ".json", ".log"}
# 附件来自 agent 和团队里的同事：这些类型用默认程序「打开」就是直接运行，只在文件夹里选中它，让用户自己决定
RUNNABLE = {".exe", ".com", ".scr", ".pif", ".cpl", ".msi", ".msp", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe",
            ".js", ".jse", ".wsf", ".wsh", ".hta", ".reg", ".lnk", ".url", ".jar", ".py", ".pyw", ".sh", ".msc",
            ".inf", ".scf", ".appref-ms", ".application", ".gadget", ".settingcontent-ms"}
SEND_BODY_MAX = 1024 * 1024


class _WriteClosed(Exception):
    pass


class WriteGate:
    """平时允许并发写请求；恢复关闸后拒绝新请求，等待已有请求退出。"""
    def __init__(self):
        self._condition = threading.Condition()
        self._active = 0
        self.closed = False

    @contextmanager
    def enter(self):
        with self._condition:
            if self.closed:
                raise _WriteClosed
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def close_and_wait(self, timeout=30):
        with self._condition:
            self.closed = True
            if not self._condition.wait_for(lambda: self._active == 0, timeout=timeout):
                raise ValueError("网页还在结束已有写入，请稍后重试恢复")


class _WebServer(ThreadingHTTPServer):
    peer_service: peer.PeerService | None = None
    wake_service: WakeService | None = None

    def pause_for_restore(self) -> None:
        # 先让两个后台服务都看到停止标记，再等待已有操作退出。
        if self.peer_service:
            self.peer_service.read_only = True
            self.peer_service._stop.set()
        if self.wake_service:
            self.wake_service.pause_for_restore()
        if self.peer_service:
            self.peer_service.pause_for_restore()
        self.write_gate.close_and_wait()

    def server_close(self) -> None:  # 网页关掉时，团队端口、后台投递线程、自动唤醒一起停
        super().server_close()
        # 端口被占用时 socketserver 在构造函数里就调用这里，那时还没有 peer_service
        if self.peer_service:
            self.peer_service.stop()
        if self.wake_service:
            self.wake_service.stop()


def _port_owner(port: int) -> str:
    """占着这个端口的程序（「Clash.exe，进程号 123」），查不到就是空字符串。"""
    try:
        import psutil
        for c in psutil.net_connections("tcp"):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
                return f"{psutil.Process(c.pid).name()}，进程号 {c.pid}"
    except Exception:
        pass
    return ""


def open_local(path: Path, folder: bool = False) -> None:
    """在本机用默认程序打开文件；folder=True 时打开它所在的文件夹并选中它。"""
    if sys.platform == "win32":
        if folder:
            # 要整行原样传：路径有空格时，列表参数会被引成 "/select,C:\..."，explorer 认不出来，打开的是「文档」。
            # explorer 成功时退出码也是 1，不看它
            subprocess.Popen(f'explorer /select,"{path}"')
        else:
            os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)] if folder else ["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent if folder else path)])


def make_server(port: int = WEB_PORT, *, peer_host: str = "0.0.0.0", peer_port: int = PEER_PORT,
                share: bool | None = None, wake: bool = False) -> tuple[ThreadingHTTPServer, str, str]:
    """起 HTTP 服务，返回 (server, token, url)。port=0 让系统随机分配（测试用）。

    share：None = 按上次在网页上的设置；True / False = 这次强制开 / 关团队连接。
    wake：是否在这个进程里运行自动唤醒（kopitiam web 默认运行；测试里默认不跑）。
    """
    db.init_db()
    try:
        db.purge_stale()  # 启动时清理很久不用、又没有消息的旧身份
    except Exception:
        pass
    token = secrets.token_urlsafe(16)
    user = db.user_id()
    service = peer.PeerService(peer_host, peer_port)

    class Handler(_Handler):
        pass

    Handler.token = token
    Handler.user_id_value = user
    Handler.service = service
    try:
        srv = _WebServer(("127.0.0.1", port), Handler)
    except OSError as e:
        owner = _port_owner(port)
        raise SystemExit(f"错误：端口 {port} 被占用（{owner or e}）。换一个端口：kopitiam web --port {port + 10}")
    srv.peer_service = service
    srv.write_gate = WriteGate()
    srv.backup = backup.BackupManager(on_restore=srv.pause_for_restore)
    if share if share is not None else db.get_meta("share") == "1":
        try:
            service.start_sharing()
        except peer.PeerError as e:
            print(f"警告：{e}，团队连接没有打开")
    service.start_worker()
    if wake:
        srv.wake_service = WakeService()
        srv.wake_service.start()
    return srv, token, f"http://127.0.0.1:{srv.server_address[1]}/?t={token}"


def run(port: int = WEB_PORT, open_browser: bool = True, *, peer_host: str = "0.0.0.0",
        peer_port: int = PEER_PORT, share: bool | None = None, wake: bool = True) -> None:
    srv, token, url = make_server(port, peer_host=peer_host, peer_port=peer_port, share=share, wake=wake)
    print(f"kopitiam web 已启动：{url}")  # 网页进程不是 MCP，可以往 stdout 打印
    if wake:
        print("自动唤醒已开启（网页上可以暂停；不要的话加 --no-wake）")
    svc = srv.peer_service
    if svc.sharing:
        addrs = ", ".join(f"{ip}:{svc.bound_port}" for ip in peer.local_addresses()) or f"本机IP:{svc.bound_port}"
        print(f"团队连接已开启：{svc.host}:{svc.bound_port}（同事用 {addrs} 连你）", flush=True)
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
    service: peer.PeerService

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
        if urlparse(self.path).path in ("/api/backup", "/api/backup/list"):
            if not self._host_ok() or not self._token_header_ok():
                return self._deny()
            try:
                return self._json(self.server.backup.status() if urlparse(self.path).path == "/api/backup"
                                  else self.server.backup.list())
            except (ValueError, OSError) as e:
                return self._json({"ok": False, "message": str(e)}, 400)
        return self._get()

    def _get(self) -> None:
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
            msgs = self._with_urls(chat.messages_after(after, 200))
            return self._json({"agents": chat.agents_brief(), "messages": msgs,
                               "restart_required": self.server.backup.read_only,
                               "groups": chat.groups_brief(), "deliveries": chat.delivery_updates(), "wake": wake_service.overview()})
        if u.path == "/api/team":
            if not self._token_header_ok():
                return self._deny()
            return self._json(self._team())
        if u.path == "/api/wake":
            if not self._token_header_ok():
                return self._deny()
            return self._json(wake_service.overview())
        if u.path == "/api/messages":
            if not self._token_header_ok():
                return self._deny()
            qs = parse_qs(u.query, keep_blank_values=True)
            if "group" in qs:
                try:
                    group = self._positive_id(qs["group"][0])
                    result = Chat().group_messages(self.user_id_value, group)
                except ValueError as e:
                    return self._json({"ok": False, "message": f"错误：{e}"}, 400)
                result["messages"] = self._with_urls(result["messages"])
                return self._json(result)
            name = qs.get("with", [""])[0]
            return self._json({"messages": self._with_urls(Chat().messages_with(name, 200))})
        if u.path == "/api/hidden":
            if not self._token_header_ok():
                return self._deny()
            return self._json({"agents": Chat().hidden_agents()})
        if u.path.startswith("/files/"):
            if parse_qs(u.query).get("t", [""])[0] != self.token:
                return self._deny()
            return self._serve_file(unquote(u.path[len("/files/"):]))
        return self._deny(404)

    def do_POST(self) -> None:
        if not self._host_ok() or not self._token_header_ok():
            return self._deny()
        if urlparse(self.path).path.startswith("/api/backup/"):
            return self._backup_post()
        if self.server.backup.read_only:
            return self._json({"ok": False, "message": backup.READ_ONLY}, 503)
        try:
            with self.server.write_gate.enter():
                if self.server.backup.read_only:
                    return self._json({"ok": False, "message": backup.READ_ONLY}, 503)
                return self._post()
        except _WriteClosed:
            return self._json({"ok": False, "message": backup.READ_ONLY}, 503)

    def _backup_post(self) -> None:
        data = self._read_json()
        if data is None:
            return
        manager = self.server.backup
        try:
            path = urlparse(self.path).path
            if path == "/api/backup/config":
                with backup._file_lock():
                    return self._json({"ok": True, "config": backup.store.save(data)})
            if path == "/api/backup/test":
                count = manager.test()
                return self._json({"ok": True, "count": count, "message": f"连接成功，现有 {count} 份备份"})
            if path == "/api/backup/run":
                if manager.read_only:
                    return self._json({"ok": False, "message": backup.READ_ONLY}, 503)
                return self._json({"ok": True, "job": manager.start("backup", with_files=data.get("files", False))})
            if path == "/api/backup/restore":
                return self._json({"ok": True, "job": manager.start("restore", name=data.get("name"),
                                                                  backup_password=data.get("backup_password"))})
            if path == "/api/backup/delete":
                manager.delete(data.get("name"))
                return self._json({"ok": True, "message": "已删除备份"})
            return self._deny(404)
        except (ValueError, OSError) as e:
            return self._json({"ok": False, "message": str(e)}, 409 if str(e) == backup.BUSY else 400)

    def _post(self) -> None:
        if not self._host_ok():
            return self._deny()
        if not self._token_header_ok():
            return self._deny()
        u = urlparse(self.path)
        if u.path == "/api/send":
            return self._api_send()
        if u.path == "/api/upload":
            return self._api_upload(parse_qs(u.query))
        if u.path == "/api/forget":
            return self._api_forget()
        handler = (self._TEAM_POSTS.get(u.path) or self._WAKE_POSTS.get(u.path) or self._AGENT_POSTS.get(u.path)
                   or self._FILE_POSTS.get(u.path) or self._GROUP_POSTS.get(u.path))
        if handler:
            data = self._read_json()
            if data is None:
                return
            try:
                return self._json({"ok": True} | (handler(self, data) or {}))
            except (peer.PeerError, ValueError) as e:
                return self._json({"ok": False, "message": f"错误：{e}"})
        return self._deny(404)

    # ---- 群聊 ----

    @staticmethod
    def _positive_id(value) -> int:
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdigit() or int(value) < 1:
            raise ValueError("群聊 id 要填正整数")
        return int(value)

    def _g_create(self, data: dict) -> dict:
        return {"group": Chat().create_group(self.user_id_value, data.get("title"), data.get("about", ""), data.get("members", []))}

    def _g_update(self, data: dict) -> dict:
        gid = self._positive_id(data.get("id"))
        return {"group": Chat().update_group(self.user_id_value, gid,
                **{k: v for k, v in data.items() if k in ("title", "about", "add", "remove", "archived")})}

    def _g_seen(self, data: dict) -> None:
        gid = self._positive_id(data.get("group"))
        upto = data.get("upto")
        if isinstance(upto, bool) or not isinstance(upto, int) or upto < 0:
            raise ValueError("upto 要填非负整数")
        Chat().seen_group(gid, upto)

    _GROUP_POSTS = {"/api/groups": _g_create, "/api/group": _g_update, "/api/seen": _g_seen}

    # ---- 自动唤醒 ----

    def _w_pause(self, data: dict) -> dict:
        detector.set_paused(bool(data.get("paused")))
        return wake_service.overview()

    def _w_rounds(self, data: dict) -> dict:
        try:
            n = int(data.get("n"))
        except (TypeError, ValueError):
            raise ValueError("来回上限要填数字") from None
        detector.set_max_rounds(n)
        return wake_service.overview() | {"message": f"已保存：agent 之间最多连续 {n} 个来回" if n else
                                          "已保存：只有你发的消息才会叫醒 agent"}

    def _w_agent(self, data: dict) -> dict:
        name = str(data.get("name") or "")
        row = next((a for a in Chat().agents_brief() if a["name"] == name), None)
        if row is None or row["kind"] in ("human", "remote"):
            raise ValueError(f"找不到 agent：{name}")
        detector.set_enabled(row["id"], bool(data.get("enabled")))
        return wake_service.overview()

    _WAKE_POSTS = {
        "/api/wake/pause": _w_pause,
        "/api/wake/rounds": _w_rounds,
        "/api/wake/agent": _w_agent,
    }

    # ---- 归档、回收站：手动把 agent 收进「已归档 / 隐藏」、放进回收站，或者放回去 ----

    def _a_archive(self, data: dict) -> dict:
        out = Chat().archive(str(data.get("name") or ""), bool(data.get("archived", True)))
        if out.startswith("错误："):
            raise ValueError(out[len("错误："):])
        return {"message": out}

    def _a_trash(self, data: dict) -> dict:
        out = Chat().trash(str(data.get("name") or ""), bool(data.get("trashed", True)))
        if out.startswith("错误："):
            raise ValueError(out[len("错误："):])
        return {"message": out}

    def _a_purge(self, data: dict) -> dict:
        out = Chat().purge(str(data.get("name") or ""))
        if out.startswith("错误："):
            raise ValueError(out[len("错误："):])
        return {"message": out}

    _AGENT_POSTS = {"/api/archive": _a_archive, "/api/trash": _a_trash, "/api/purge": _a_purge}

    # ---- 附件：在本机打开文件 / 文件夹（只限 FILES_DIR 里的附件） ----

    def _f_open(self, data: dict) -> dict:
        name = Path(str(data.get("name") or "")).name
        root = files_dir().resolve()
        path = (files_dir() / name).resolve()
        if not name or not path.is_relative_to(root) or not path.is_file():
            raise ValueError("找不到这个附件（可能已经删除了）")
        folder = bool(data.get("folder"))
        runnable = not folder and path.suffix.lower() in RUNNABLE
        try:
            open_local(path, folder or runnable)
        except OSError as e:
            raise ValueError(f"打不开：{e}") from e
        if runnable:
            return {"message": f"{path.suffix} 文件打开就会直接运行。为了安全，只在文件夹里选中了它，确认没问题再自己打开。",
                    "revealed": True}
        return {"message": "已打开文件夹" if folder else "已打开"}

    _FILE_POSTS = {"/api/open": _f_open}

    # ---- 团队 ----

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > SEND_BODY_MAX:
            self._json({"ok": False, "error": "请求体太大"}, 413)
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._json({"ok": False, "error": "bad json"}, 400)
            return None
        return data if isinstance(data, dict) else {}

    def _team(self) -> dict:
        svc = self.service
        return {
            "me": {"name": db.display_name()},
            "share": {"on": svc.sharing, "port": svc.bound_port or svc.port, "host": svc.host,
                      "addresses": peer.local_addresses()},
            "code": svc.code_status(),
            "peers": [{"alias": p["alias"], "name": p["name"], "address": p["address"], "online": db.peer_online(p),
                       "error": svc.ping_errors.get(p["id"])}
                      for p in db.list_peers()],
        }

    def _t_share(self, data: dict) -> dict:
        if data.get("on"):
            self.service.start_sharing()
        else:
            self.service.stop_sharing()
        return self._team()

    def _t_code(self, data: dict) -> dict:
        code, ttl = self.service.new_code()
        return {"code": code, "expires_in": ttl}

    def _t_join(self, data: dict) -> dict:
        alias = self.service.join(str(data.get("address") or ""), str(data.get("code") or ""))
        msg = f"已配对：{alias}"
        if not self.service.sharing:  # 我去加别人不需要开团队连接，但别人连不进来，发给我的消息会一直排队
            msg += "。你还没打开「团队连接」，对方发给你的消息收不到：打开上面的开关就行"
        return {"message": msg, "alias": alias}

    def _t_remove(self, data: dict) -> dict:
        name = str(data.get("name") or "")
        self.service.unpair(name)
        return {"message": f"已解除配对：{name}"}

    def _t_me(self, data: dict) -> dict:
        out = db.set_display_name(str(data.get("name") or ""))
        if out.startswith("错误："):
            raise peer.PeerError(out[len("错误："):])
        return {"message": out}

    def _t_forward(self, data: dict) -> dict:
        try:
            mid = int(data.get("id"))
        except (TypeError, ValueError):
            raise peer.PeerError("消息 id 不对") from None
        out = Chat().forward(self.user_id_value, mid, [str(t) for t in (data.get("to") or [])])
        if out.startswith("错误："):
            raise peer.PeerError(out[len("错误："):])
        return {"message": out}

    _TEAM_POSTS = {
        "/api/team/share": _t_share,
        "/api/team/code": _t_code,
        "/api/team/join": _t_join,
        "/api/team/remove": _t_remove,
        "/api/team/me": _t_me,
        "/api/forward": _t_forward,
    }

    def _with_urls(self, msgs: list[dict]) -> list[dict]:
        for m in msgs:
            for f in m["files"]:
                f["url"] = f"/files/{quote(Path(f['path']).name)}?t={self.token}"
        return msgs

    def _api_forget(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > SEND_BODY_MAX:
            return self._json({"ok": False, "error": "请求体太大"}, 413)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._json({"ok": False, "error": "bad json"}, 400)
        out = Chat().forget(str(data.get("name") or ""))
        self._json({"ok": not out.startswith("错误："), "message": out})

    # ---- 接口 ----

    def _api_send(self) -> None:
        data = self._read_json()
        if data is None:
            return
        group = None
        if data.get("group") is not None:
            try:
                group = self._positive_id(data["group"])
            except ValueError as e:
                return self._json({"ok": False, "message": f"错误：{e}"}, 400)
            if "to" in data:
                return self._json({"ok": False, "message": "错误：群聊里用 @名字 点名，不用 to。"})
        text = str(data.get("text") or "")
        to = None if group is not None else [str(t) for t in (data.get("to") or [])]
        files = [str(f) for f in (data.get("files") or [])]
        out = Chat().send(self.user_id_value, text, to, files, group)
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
<script>
  // 尽早套用上次选的主题，避免页面先闪一下默认配色（读不到就跟随系统）
  try { const t = localStorage.getItem("kopitiam-theme"); if (t && t !== "auto") document.documentElement.dataset.theme = t; } catch (e) {}
</script>
<style>
/* ---- 主题：不设 data-theme = 跟随系统（浅色 / 曜石）；其余主题各自一套颜色 ---- */
:root, [data-theme="light"] {
  color-scheme: light;
  --bg:#f3f4f6; --panel:#ffffff; --hover:#f2f3f5; --ink:#1d2129; --ink-2:#4e5969; --muted:#86909c; --line:#e5e6eb;
  --accent:#3370ff; --accent-ink:#ffffff; --accent-soft:rgba(51,112,255,.10); --ok:#00b42a; --name-l:40%; --shadow:0 1px 2px rgba(15,23,42,.06);
  --mine-bg:#3370ff; --mine-ink:#ffffff; --g1:#3370ff; --g2:#8b5cf6; --g3:#06b6d4; --glow:rgba(51,112,255,.35);
  --avb-s:55%; --avb-l:52%; --avf-s:0%; --avf-l:100%;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme]) {
    color-scheme: dark;
    --bg:#000000; --panel:#0c0c0e; --hover:#17171a; --ink:#f4f4f5; --ink-2:#a1a1aa; --muted:#71717a; --line:#1f1f23;
    --accent:#fafafa; --accent-ink:#0a0a0a; --accent-soft:rgba(255,255,255,.07); --ok:#22c55e; --name-l:78%; --shadow:none;
    --mine-bg:#1c1c1f; --mine-ink:#f4f4f5; --g1:#5b8cff; --g2:#a855f7; --g3:#f472b6; --glow:rgba(168,85,247,.30);
    --avb-s:30%; --avb-l:18%; --avf-s:75%; --avf-l:78%;
  }
}
[data-theme="obsidian"] {  /* 曜石：纯黑 + 白色强调，极简 */
  color-scheme: dark;
  --bg:#000000; --panel:#0c0c0e; --hover:#17171a; --ink:#f4f4f5; --ink-2:#a1a1aa; --muted:#71717a; --line:#1f1f23;
  --accent:#fafafa; --accent-ink:#0a0a0a; --accent-soft:rgba(255,255,255,.07); --ok:#22c55e; --name-l:78%; --shadow:none;
  --mine-bg:#1c1c1f; --mine-ink:#f4f4f5; --g1:#5b8cff; --g2:#a855f7; --g3:#f472b6; --glow:rgba(168,85,247,.30);
  --avb-s:30%; --avb-l:18%; --avf-s:75%; --avf-l:78%;
}
[data-theme="dark"] {
  color-scheme: dark;
  --bg:#0f1115; --panel:#171a1f; --hover:#1f232a; --ink:#e6e8eb; --ink-2:#b4bac3; --muted:#7d8590; --line:#272b32;
  --accent:#5b8dff; --accent-ink:#ffffff; --accent-soft:rgba(91,141,255,.16); --ok:#3ccf62; --name-l:70%; --shadow:none;
  --mine-bg:#5b8dff; --mine-ink:#ffffff; --g1:#5b8dff; --g2:#8b5cf6; --g3:#22d3ee; --glow:rgba(91,141,255,.30);
  --avb-s:55%; --avb-l:52%; --avf-s:0%; --avf-l:100%;
}
[data-theme="latte"] {  /* 拿铁：奶油底 + 焦糖色 */
  color-scheme: light;
  --bg:#f5ede3; --panel:#fffaf4; --hover:#f0e5d7; --ink:#3b2a1f; --ink-2:#6b5444; --muted:#9c8573; --line:#e7d9c8;
  --accent:#a8612a; --accent-ink:#ffffff; --accent-soft:rgba(168,97,42,.12); --ok:#4f8a3c; --name-l:36%; --shadow:0 1px 2px rgba(80,50,20,.08);
  --mine-bg:#a8612a; --mine-ink:#ffffff; --g1:#a8612a; --g2:#d99a55; --g3:#e8c39e; --glow:rgba(168,97,42,.30);
  --avb-s:55%; --avb-l:52%; --avf-s:0%; --avf-l:100%;
}
[data-theme="kopi"] {  /* 咖啡：深棕底 + 琥珀色 */
  color-scheme: dark;
  --bg:#17110e; --panel:#211915; --hover:#2c221d; --ink:#f2e7dc; --ink-2:#cfbba9; --muted:#9a8574; --line:#382d27;
  --accent:#d99a55; --accent-ink:#2b1a0b; --accent-soft:rgba(217,154,85,.16); --ok:#86c46a; --name-l:72%; --shadow:none;
  --mine-bg:#d99a55; --mine-ink:#2b1a0b; --g1:#d99a55; --g2:#f0c27b; --g3:#a8612a; --glow:rgba(217,154,85,.30);
  --avb-s:55%; --avb-l:52%; --avf-s:0%; --avf-l:100%;
}
[data-theme="ocean"] {  /* 深海：深蓝底 + 青绿色 */
  color-scheme: dark;
  --bg:#0b1220; --panel:#111a2c; --hover:#18233a; --ink:#e3eaf7; --ink-2:#b2c0da; --muted:#7b8aa8; --line:#223049;
  --accent:#3cc8bd; --accent-ink:#04201d; --accent-soft:rgba(60,200,189,.15); --ok:#4ade80; --name-l:72%; --shadow:none;
  --mine-bg:#3cc8bd; --mine-ink:#04201d; --g1:#3cc8bd; --g2:#3b82f6; --g3:#a78bfa; --glow:rgba(60,200,189,.30);
  --avb-s:55%; --avb-l:52%; --avf-s:0%; --avf-l:100%;
}
[data-theme="sakura"] {  /* 樱花：浅粉底 + 玫红色 */
  color-scheme: light;
  --bg:#fbf1f4; --panel:#ffffff; --hover:#f8e6ec; --ink:#3a2530; --ink-2:#6d4e5c; --muted:#a08592; --line:#f0d9e2;
  --accent:#d9466f; --accent-ink:#ffffff; --accent-soft:rgba(217,70,111,.10); --ok:#3fa564; --name-l:40%; --shadow:0 1px 2px rgba(120,40,70,.07);
  --mine-bg:#d9466f; --mine-ink:#ffffff; --g1:#d9466f; --g2:#f59ec0; --g3:#a78bfa; --glow:rgba(217,70,111,.28);
  --avb-s:55%; --avb-l:52%; --avf-s:0%; --avf-l:100%;
}
/* 机器人头像的底色和头部颜色（色相按名字算，这里只定饱和度/亮度） */
/* 用户小人：肤色、发色等；深色主题里头发调浅，免得和黑底糊在一起 */
:root { --bot-head-s:62%; --bot-head-l:58%; --skin:#f7d5bb; --hair:#3f2b22; --hair-l:32%; --eye-ink:#2b1d16; --mouth:#a2533f; }
[data-theme="obsidian"] { --bot-head-s:48%; --bot-head-l:50%; --hair:#d6b08c; --hair-l:66%; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme]) { --bot-head-s:48%; --bot-head-l:50%; --hair:#d6b08c; --hair-l:66%; }
}
[data-theme="dark"], [data-theme="kopi"], [data-theme="ocean"] { --bot-head-s:54%; --bot-head-l:54%; --hair:#d6b08c; --hair-l:66%; }

/* ---- 动效 ---- */
@property --angle { syntax:"<angle>"; inherits:false; initial-value:0deg; }
@keyframes spin { to { --angle:360deg; } }
@keyframes blink { 0%, 92%, 100% { transform:scaleY(1); } 95% { transform:scaleY(.1); } }
@keyframes look { 0%, 34%, 100% { transform:translateX(0); } 40%, 54% { transform:translateX(1.8px); } 60%, 74% { transform:translateX(-1.8px); } 80% { transform:translateX(0); } }
@keyframes beacon { 0%, 100% { opacity:1; filter:drop-shadow(0 0 2.5px var(--ok)); } 50% { opacity:.5; filter:none; } }
@keyframes talk { from { transform:scaleY(1); } to { transform:scaleY(3.4) scaleX(1.15); } }
@keyframes chatter { from { transform:translateY(0); } to { transform:translateY(-1.2px); } }
@keyframes zz { 0% { opacity:0; transform:translate(0, 4px) scale(.7); } 30% { opacity:1; } 100% { opacity:0; transform:translate(5px, -7px) scale(1.15); } }
@keyframes bob { 0%, 100% { transform:translateY(0); } 50% { transform:translateY(-1.5px); } }
@keyframes sway { 0%, 100% { transform:rotate(-10deg); } 50% { transform:rotate(12deg); } }
@keyframes rise { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:none; } }
@keyframes pop { from { opacity:0; transform:translateY(6px) scale(.98); } to { opacity:1; transform:none; } }

* { box-sizing:border-box; }
[hidden] { display:none !important; }
html, body { height:100%; }
body {
  margin:0; display:flex; background:var(--bg); color:var(--ink);
  font:14px/1.5 "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, "Microsoft YaHei UI", "PingFang SC", sans-serif;
  -webkit-font-smoothing:antialiased; transition:background-color .25s, color .25s;
}
button, input, textarea { font:inherit; color:inherit; }
::placeholder { color:var(--muted); }
::-webkit-scrollbar { width:10px; height:10px; }
::-webkit-scrollbar-thumb { background:var(--line); border-radius:10px; border:3px solid transparent; background-clip:content-box; }
.ic { display:inline-grid; place-items:center; flex-shrink:0; }
.ic svg { display:block; }

/* ---- logo：线条咖啡杯 + 旋转光晕 ---- */
.mark { position:relative; width:34px; height:34px; flex-shrink:0; display:grid; place-items:center; color:var(--ink); }
.mark::before {
  content:""; position:absolute; inset:-5px; z-index:0; border-radius:50%;
  background:conic-gradient(from var(--angle), var(--g1), var(--g2), var(--g3), var(--g1));
  filter:blur(11px); opacity:.45; animation:spin 9s linear infinite;
}
.mark::after { content:""; position:absolute; inset:0; z-index:0; border-radius:11px; background:var(--panel); border:1px solid var(--line); }
.mark svg { position:relative; z-index:1; }

/* ---- 左栏 ---- */
aside { width:280px; flex-shrink:0; display:flex; flex-direction:column; background:var(--panel); border-right:1px solid var(--line); }
.brand { display:flex; align-items:center; gap:12px; padding:18px 20px 12px; }
.brand .logo { font-size:16px; font-weight:700; letter-spacing:.3px; }
.brand .sub { font-size:12px; color:var(--muted); }
#sideScroll { flex:1; min-height:0; overflow-y:auto; padding:2px 10px 16px; }
.section { display:flex; align-items:center; gap:6px; padding:14px 10px 6px; font-size:12px; font-weight:600; color:var(--muted); letter-spacing:.3px; }
details.offline > summary { list-style:none; cursor:pointer; user-select:none; }
details.offline > summary::-webkit-details-marker { display:none; }
details.offline > summary .ic { transition:transform .15s; }
details.offline[open] > summary .ic { transform:rotate(90deg); }
details.offline > summary:hover { color:var(--ink-2); }

.agent { display:flex; align-items:center; gap:11px; padding:8px 10px; margin:2px 0; border-radius:12px; cursor:pointer; transition:background-color .15s; }
.agent:hover { background:var(--hover); }
.agent.active { background:var(--accent-soft); }
.agent.active .name { color:var(--accent); }
.avatar {
  position:relative; flex-shrink:0; width:34px; height:34px; border-radius:50%;
  display:grid; place-items:center; font-size:14px; font-weight:600;
  background:hsl(var(--h) var(--avb-s) var(--avb-l)); color:hsl(var(--h) var(--avf-s) var(--avf-l));
}
.agent.off .avatar { filter:grayscale(1); opacity:.45; }

/* ---- 机器人头像：在线 = 醒着（眨眼、东张西望、天线灯呼吸），离线 = 睡觉，发消息时张嘴说话 ---- */
.bot, .human { background:none; border-radius:0; overflow:visible; }
.bot svg, .human svg { width:100%; height:100%; overflow:visible; transition:transform .3s cubic-bezier(.3, 1.6, .5, 1); }
.agent:hover .bot svg, .agent:hover .human svg { transform:rotate(-9deg) translateY(-1px); }

/* 用户小人：眨眼、呆毛摇摆、轻轻浮动 */
.human .skin { fill:var(--skin); }
.human .hair { fill:var(--hair); }
.human.peer .hair { fill:hsl(var(--hh) 42% var(--hair-l)); }  /* 同事：发色按名字变 */
.human.peer .strand { stroke:hsl(var(--hh) 42% var(--hair-l)); }
.human .strand { fill:none; stroke:var(--hair); stroke-width:1.9; stroke-linecap:round; transform-box:fill-box; transform-origin:0% 100%; animation:sway 3.2s ease-in-out infinite; }
.human .eyeh { fill:var(--eye-ink); }
.human .lids { transform-box:fill-box; transform-origin:center; animation:blink 4.8s infinite; animation-delay:-1.3s; }
.human .cheek { fill:rgba(255, 110, 120, .38); }
.human .smile { fill:none; stroke:var(--mouth); stroke-width:1.4; stroke-linecap:round; }
.human .face { animation:bob 4.2s ease-in-out infinite; }
.bot .head { fill:hsl(var(--h) var(--bot-head-s) var(--bot-head-l)); }
.bot .ear { fill:hsl(var(--h) var(--bot-head-s) calc(var(--bot-head-l) - 12%)); }
.bot .stalk { stroke:hsl(var(--h) var(--bot-head-s) calc(var(--bot-head-l) - 12%)); stroke-width:1.8; stroke-linecap:round; }
.bot .visor { fill:#0d0e14; }
.bot .eye { fill:hsl(var(--h) 100% 76%); filter:drop-shadow(0 0 1.6px hsl(var(--h) 100% 66%)); }
.bot .mouth { fill:hsl(var(--h) 100% 76%); opacity:.85; }
.bot .mouth.s { fill:none; stroke:hsl(var(--h) 100% 76%); stroke-width:1.4; stroke-linecap:round; }
.bot .sleep { fill:none; stroke:hsl(var(--h) 40% 70%); stroke-width:1.6; stroke-linecap:round; display:none; }
.bot .light { fill:var(--muted); }
.bot .eye, .bot .mouth, .bot .lids, .bot .eyes { transform-box:fill-box; transform-origin:center; }
.bot.on .light { fill:var(--ok); animation:beacon 2.4s ease-in-out infinite; animation-delay:var(--d); }
/* 被自动唤醒：一跳，天线灯闪成琥珀色 */
@keyframes jolt { 0%, 100% { transform:none; } 35% { transform:translateY(-3px) scale(1.07); } 70% { transform:scale(.97); } }
@keyframes zap { 0%, 100% { fill:var(--ok); } 50% { fill:#ffb020; } }
.bot.woke svg { animation:jolt .55s cubic-bezier(.3, 1.6, .5, 1) 2; }
.bot.woke .light { animation:zap .3s linear 8; }
.bot.on .lids { animation:blink 5.2s infinite; animation-delay:var(--d); }
.bot.on .eyes { animation:look 9s ease-in-out infinite; animation-delay:var(--d2); }
.bot.on .face { animation:bob 3.6s ease-in-out infinite; animation-delay:var(--d2); }
.bot.talk .mouth { animation:talk .16s ease-in-out infinite alternate; }
.bot.on.talk .face, .bot.talk .face { animation:chatter .16s ease-in-out infinite alternate; }
.agent .avatar { width:40px; height:40px; font-size:15px; }
.bot.off .eyes { display:none; }
.bot.off .sleep { display:inline; }
.bot .z { position:absolute; right:-4px; top:-8px; font-size:11px; font-weight:700; color:var(--muted); pointer-events:none; animation:zz 3.2s ease-in-out infinite; animation-delay:var(--d); }
.bot.still *, .bot.still svg { animation:none !important; }
.bot.still .light { fill:hsl(var(--h) 80% 72%); }  /* 消息里的头像不表示在线状态，天线灯用中性色 */
#convAvatar .avatar { width:34px; height:34px; }
#emptyArt .bot { width:72px; height:72px; }
.agent .info { flex:1; min-width:0; }
.agent .line1 { display:flex; align-items:center; gap:6px; min-width:0; }
.agent .name { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.kind { flex-shrink:0; padding:0 6px; border-radius:5px; font-size:11px; line-height:17px; color:var(--ink-2); background:var(--hover); }
.agent:hover .kind, .agent.active .kind { background:var(--panel); }
/* 头像右下角的小闪电：收到点名消息会被自动唤醒（灰 = 暂停 / 没在运行，琥珀 = 来回超过上限） */
.wk { position:absolute; right:-4px; bottom:-3px; z-index:1; display:grid; place-items:center; width:15px; height:15px;
  border-radius:50%; background:var(--panel); box-shadow:0 0 0 1.5px var(--panel); color:var(--ok); }
.wk svg { width:10px; height:10px; fill:currentColor; stroke-width:1.2; }
.wk.idle { color:var(--muted); }
.wk.limit { color:#e5a00d; }
.agent:hover .wk, .agent.active .wk { background:var(--hover); box-shadow:0 0 0 1.5px var(--hover); }
.agent.active .wk { background:var(--panel); box-shadow:0 0 0 1.5px var(--panel); }
.agent .cwd { margin-top:1px; font-size:12px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.agent .cwd.warn { color:#e5a00d; }
/* 成员行右边的按钮（加到「发给」、归档）：能悬停的设备上平时不占宽度，名字能完整显示 */
.agent .ops { flex-shrink:0; display:flex; gap:4px; }
.agent .act {
  width:28px; height:28px; display:grid; place-items:center; cursor:pointer;
  border:1px solid var(--line); border-radius:9px; background:var(--panel); color:var(--ink-2); transition:color .15s, border-color .15s;
}
.agent .act:hover { color:var(--accent); border-color:var(--accent); }
@media (hover:hover) {
  .agent .ops { width:0; margin-left:-11px; overflow:hidden; opacity:0; }
  .agent:hover .ops, .agent .ops:focus-within { width:auto; margin-left:0; opacity:1; }
}

/* 主题：默认收起，只留一个按钮，点开才弹出 */
#themes { position:relative; flex-shrink:0; padding:8px 10px 12px; border-top:1px solid var(--line); }
.sidebtn { width:100%; display:flex; align-items:center; gap:9px; padding:8px 10px; border:0; border-radius:10px; background:transparent; color:var(--ink-2); font-size:13px; cursor:pointer; }
.sidebtn:hover { background:var(--hover); color:var(--ink); }
.sidebtn .cur { margin-left:auto; font-size:12px; color:var(--muted); }
.sidebtn .cur.warn { color:#e5a00d; }
#themePop {
  position:absolute; left:10px; right:10px; bottom:calc(100% - 2px); z-index:30;
  display:grid; grid-template-columns:1fr 1fr; gap:2px; padding:8px;
  background:var(--panel); border:1px solid var(--line); border-radius:14px; box-shadow:0 16px 48px rgba(0,0,0,.28);
  animation:pop .16s ease-out;
}
.theme-opt { display:flex; align-items:center; gap:9px; padding:7px 8px; border:0; border-radius:9px; background:transparent; color:var(--ink); font-size:13px; text-align:left; cursor:pointer; }
.theme-opt:hover { background:var(--hover); }
.theme-opt.on { background:var(--accent-soft); }
.swatch { width:18px; height:18px; flex-shrink:0; border-radius:50%; box-shadow:0 0 0 1px var(--line); }

/* ---- 主区 ---- */
main { flex:1; min-width:0; display:flex; flex-direction:column; }
#convbar { flex-shrink:0; display:flex; align-items:center; gap:12px; height:58px; padding:0 24px; background:var(--panel); border-bottom:1px solid var(--line); }
#convbar .titles { flex:1; min-width:0; }
#convtitle { font-size:15px; font-weight:600; }
#convsub { font-size:12px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.btn { display:inline-flex; align-items:center; gap:6px; padding:5px 12px; border:1px solid var(--line); border-radius:999px; background:var(--panel); color:var(--ink); font-size:13px; cursor:pointer; transition:background-color .15s; }
.btn:hover { background:var(--hover); }
#menuBtn { display:none; width:36px; height:36px; padding:0; justify-content:center; }

#stream { flex:1; overflow-y:auto; padding:24px 24px 8px; }

/* 连不上网页服务时的提示条（服务关了、或重启后 token 变了） */
#conn { flex-shrink:0; display:flex; align-items:center; gap:10px; padding:9px 24px; font-size:12.5px; color:var(--ink-2); background:var(--hover); border-bottom:1px solid var(--line); }
#conn .dot { flex-shrink:0; width:8px; height:8px; border-radius:50%; background:#f59e0b; box-shadow:0 0 0 3px rgba(245,158,11,.18); animation:beacon 1.6s ease-in-out infinite; }

/* ---- 「已隐藏」子页面 ---- */
main.page > #convbar, main.page > #conn, main.page > #stream, main.page > #composer { display:none; }
#hiddenPage { flex:1; min-height:0; display:flex; flex-direction:column; }
.pagebar { flex-shrink:0; display:flex; align-items:center; gap:14px; min-height:58px; padding:8px 24px; background:var(--panel); border-bottom:1px solid var(--line); }
.pagebar .titles { flex:1; min-width:0; }
.ptitle { font-size:15px; font-weight:600; }
.psub { font-size:12px; color:var(--muted); }
#hiddenList { flex:1; overflow-y:auto; padding:22px 24px; }
.hcard { max-width:860px; margin:0 auto 10px; display:flex; align-items:center; gap:14px; padding:14px 16px; border:1px solid var(--line); border-radius:16px; background:var(--panel); animation:rise .25s ease-out both; }
.hcard .avatar { width:44px; height:44px; filter:grayscale(1); opacity:.55; }
.hcard .info { flex:1; min-width:0; }
.hcard .line1 { display:flex; align-items:center; gap:6px; }
.hcard .name { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.hcard .cwd { font-size:12px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.hmeta { margin-top:3px; font-size:12px; color:var(--ink-2); }
.acts { flex-shrink:0; display:flex; align-items:center; gap:8px; }
.acts .keep { font-size:12px; color:var(--muted); }
.btn.danger { color:#e5484d; }
.btn.danger:hover { border-color:#e5484d; }
.hsec { max-width:860px; margin:22px auto 10px; display:flex; align-items:baseline; gap:10px; font-size:12px; font-weight:600; color:var(--muted); letter-spacing:.3px; }
.hsec .note { font-weight:400; letter-spacing:0; }
.hcard.trash { border-style:dashed; }
.hempty { margin-top:16vh; text-align:center; font-size:13px; color:var(--muted); }
.hidden-link { width:100%; display:flex; align-items:center; gap:8px; margin-top:10px; padding:9px 10px; border:0; border-top:1px solid var(--line); background:transparent; color:var(--muted); font-size:12px; font-weight:600; letter-spacing:.3px; cursor:pointer; }
.hidden-link:hover { color:var(--ink-2); }
.hidden-link .grow { flex:1; text-align:left; }

/* ---- 团队 ---- */
.team-head .grow { flex:1; }
.mini { display:grid; place-items:center; width:22px; height:22px; padding:0; border:1px solid var(--line); border-radius:7px; background:transparent; color:var(--muted); cursor:pointer; }
.mini:hover { color:var(--accent); border-color:var(--accent); }
.team-hint { width:100%; display:flex; align-items:center; gap:8px; margin:2px 0; padding:9px 10px; border:1px dashed var(--line); border-radius:12px; background:transparent; color:var(--muted); font-size:12.5px; cursor:pointer; }
.team-hint:hover { color:var(--ink-2); border-color:var(--muted); }
.meta .fwdbtn { display:inline-grid; place-items:center; width:22px; height:22px; padding:0; border:0; border-radius:6px; background:transparent; color:var(--muted); cursor:pointer; opacity:0; transition:opacity .15s; }
.msg:hover .fwdbtn, .meta .fwdbtn:focus-visible { opacity:1; }
.meta .fwdbtn:hover { color:var(--accent); background:var(--hover); }
@media (hover:none) { .meta .fwdbtn { opacity:1; } }
.dstate { display:flex; flex-wrap:wrap; align-items:center; gap:4px; margin-top:4px; font-size:11.5px; color:var(--muted); }
.dstate .derr { flex-basis:100%; color:#e5a00d; line-height:1.5; overflow-wrap:anywhere; }
.msg.mine .dstate { justify-content:flex-end; }
.msg.mine .dstate .derr { text-align:right; }
.dstate.sent { color:var(--ok); }

/* ---- 弹窗 ---- */
@keyframes fade { from { opacity:0; } to { opacity:1; } }
#modal { position:fixed; inset:0; z-index:50; display:grid; place-items:center; padding:16px; background:rgba(0,0,0,.45); animation:fade .15s ease-out; }
.mcard { width:min(540px, 100%); max-height:min(88vh, 780px); overflow-y:auto; border:1px solid var(--line); border-radius:18px; background:var(--panel); box-shadow:0 24px 64px rgba(0,0,0,.35); animation:pop .18s ease-out; }
.mhead { position:sticky; top:0; z-index:1; display:flex; align-items:center; justify-content:space-between; padding:12px 14px 10px 20px; border-bottom:1px solid var(--line); background:var(--panel); font-size:15px; font-weight:600; }
#mbody { padding:4px 20px 18px; }
.tsec { padding:14px 0; border-bottom:1px solid var(--line); }
.tsec:last-child { border-bottom:0; }
.tsec h3 { margin:0 0 2px; font-size:13px; font-weight:600; }
.tsub { margin-bottom:10px; font-size:12px; color:var(--muted); }
.trow { display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
.trow .grow { flex:1; }
.tin { flex:1; min-width:120px; padding:7px 12px; border:1px solid var(--line); border-radius:10px; background:var(--bg); color:var(--ink); outline:0; }
.tin:focus { border-color:var(--accent); }
.code-in { flex:0 0 120px; min-width:0; letter-spacing:2px; }
.code { padding:8px 16px; border-radius:12px; background:var(--hover); font:600 28px/1.2 "Cascadia Mono", Consolas, ui-monospace, monospace; letter-spacing:7px; user-select:all; }
.addr { padding:4px 0 6px; font:600 15px "Cascadia Mono", Consolas, ui-monospace, monospace; color:var(--ink); user-select:all; word-break:break-all; }
.addr + .mnote { margin-bottom:10px; }
.mlabel { margin-top:10px; font-size:12px; color:var(--muted); }
.mnote { font-size:12px; line-height:1.65; color:var(--muted); }
.mok { margin-top:8px; font-size:12px; color:var(--ok); }
.merr { margin-top:8px; font-size:12px; color:#e5484d; }
.switch { position:relative; flex-shrink:0; width:40px; height:22px; padding:0; border:0; border-radius:999px; background:var(--line); cursor:pointer; transition:background-color .2s; }
.switch::after { content:""; position:absolute; top:3px; left:3px; width:16px; height:16px; border-radius:50%; background:#fff; box-shadow:0 1px 3px rgba(0,0,0,.3); transition:transform .2s; }
.switch.on { background:var(--ok); }
.switch.on::after { transform:translateX(18px); }
.prow { display:flex; align-items:center; gap:10px; padding:8px 0; }
.prow .avatar { width:32px; height:32px; }
.prow .info { flex:1; min-width:0; }
.prow .name { font-weight:600; }
.prow .cwd { font-size:12px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.prow .cwd.warn { color:#e5a00d; }
.prow .perr { margin-top:2px; font-size:12px; line-height:1.5; color:#e5a00d; overflow-wrap:anywhere; }
.prow .line1 { display:flex; align-items:center; gap:6px; }
.rounds { flex:0 0 84px; min-width:0; }
.fwd-preview { margin:8px 0 4px; padding:10px 12px; border-left:3px solid var(--line); border-radius:6px; background:var(--hover); font-size:13px; color:var(--ink-2); white-space:pre-wrap; word-break:break-word; }
.pick { display:flex; flex-direction:column; gap:2px; max-height:300px; overflow-y:auto; margin:10px 0; }
.pick-row { display:flex; align-items:center; gap:10px; padding:7px 8px; border-radius:10px; cursor:pointer; }
.pick-row:hover { background:var(--hover); }
.pick-row input { width:16px; height:16px; accent-color:var(--accent); }
.pick-row .avatar { width:26px; height:26px; }
.pick-row .name { flex:1; font-weight:500; }
.bigbtn { width:100%; margin-top:12px; padding:9px; border:0; border-radius:10px; background:var(--accent); color:var(--accent-ink); font-weight:600; cursor:pointer; }
.bigbtn:disabled { opacity:.35; cursor:default; }
#list, #groupList { max-width:860px; margin:0 auto; }
.enter { animation:rise .3s cubic-bezier(.2, .7, .3, 1) both; }
#empty { display:flex; flex-direction:column; align-items:center; gap:14px; margin-top:16vh; text-align:center; }
#empty .mark { width:64px; height:64px; }
#empty .mark::before { inset:-14px; filter:blur(22px); opacity:.55; }
#empty .mark::after { border-radius:20px; }
#emptyTitle { font-size:20px; font-weight:700; letter-spacing:.3px; }
#emptySub { font-size:13px; color:var(--muted); }

.msg { display:flex; align-items:flex-start; gap:10px; margin:0 0 20px; }
.msg .avatar { width:32px; height:32px; font-size:13px; }
.msg .body { min-width:0; max-width:min(640px, 78%); display:flex; flex-direction:column; align-items:flex-start; }
.meta { display:flex; align-items:baseline; flex-wrap:wrap; gap:6px; margin-bottom:5px; font-size:12px; color:var(--muted); }
.meta .who { font-size:13px; font-weight:600; color:hsl(var(--h) 60% var(--name-l)); }
.meta .who.copyable { cursor:pointer; }
.meta .who.copyable:hover { text-decoration:underline; }
.meta .id { opacity:.65; }
.bubble {
  padding:10px 14px; border:1px solid var(--line); border-radius:6px 18px 18px 18px;
  background:var(--panel); box-shadow:var(--shadow); line-height:1.65; white-space:pre-wrap; word-break:break-word;
}
.msg.mine { flex-direction:row-reverse; }
.msg.mine .body { align-items:flex-end; }
.msg.mine .meta { justify-content:flex-end; }
.msg.mine .bubble { background:var(--mine-bg); color:var(--mine-ink); border-color:transparent; border-radius:18px 6px 18px 18px; }
.notice { margin:2px 0 20px; text-align:center; }
.notice span { padding:4px 12px; border-radius:999px; font-size:12px; color:var(--muted); background:var(--hover); }
.files { display:flex; flex-direction:column; gap:6px; margin-top:6px; max-width:100%; }
.file { display:flex; align-items:center; gap:10px; min-width:250px; max-width:100%; padding:9px 10px 9px 12px; border:1px solid var(--line); border-radius:14px; background:var(--panel); transition:border-color .15s; }
.file:hover { border-color:var(--muted); }
.file > .ic { width:32px; height:32px; border-radius:9px; background:var(--hover); color:var(--ink-2); }
.file .fn { flex:1; min-width:0; }
.file a { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--ink); font-weight:500; text-decoration:none; }
.file a:hover { color:var(--accent); text-decoration:underline; }
.file .sz { font-size:11.5px; color:var(--muted); }
.file .btn { flex-shrink:0; padding:4px 10px; font-size:12px; }
/* 附件的分体按钮：左边「打开」，右边向下箭头弹出「打开文件夹」「复制路径」 */
.split { position:relative; flex-shrink:0; display:inline-flex; }
.split > .btn.main { border-radius:999px 0 0 999px; padding-right:9px; }
.split > .btn.more { margin-left:-1px; border-radius:0 999px 999px 0; padding:4px 7px 4px 6px; }
.fmenu { position:absolute; right:0; top:calc(100% + 6px); z-index:20; min-width:148px; padding:5px;
  background:var(--panel); border:1px solid var(--line); border-radius:12px; box-shadow:0 16px 48px rgba(0,0,0,.28); animation:pop .14s ease-out; }
.fmenu.up { top:auto; bottom:calc(100% + 6px); }
.fmenu button { display:flex; align-items:center; gap:8px; width:100%; padding:7px 10px; border:0; border-radius:8px;
  background:transparent; color:var(--ink); font-size:13px; text-align:left; white-space:nowrap; cursor:pointer; }
.fmenu button:hover { background:var(--hover); }

/* ---- 输入区：圆角浮动卡片，获得焦点时边框是流动的彩色光 ---- */
#composer { flex-shrink:0; padding:10px 24px 20px; }
.box {
  max-width:860px; margin:0 auto; border:1px solid var(--line); border-radius:22px;
  background:var(--panel); box-shadow:var(--shadow); transition:box-shadow .25s;
}
.box:focus-within {
  border-color:transparent;
  background:linear-gradient(var(--panel), var(--panel)) padding-box,
             conic-gradient(from var(--angle), var(--g1), var(--g2), var(--g3), var(--g1)) border-box;
  box-shadow:0 12px 44px -14px var(--glow);
  animation:spin 5s linear infinite;
}
.torow { display:flex; align-items:center; gap:10px; padding:10px 18px 8px; border-bottom:1px solid var(--line); }
.torow .lbl { flex-shrink:0; font-size:12px; color:var(--muted); }
#to { flex:1; min-width:0; padding:2px 0; border:0; outline:0; background:transparent; }
#text { display:block; width:100%; min-height:72px; max-height:40vh; padding:10px 18px; border:0; outline:0; resize:none; background:transparent; line-height:1.6; }
.tools { display:flex; align-items:center; flex-wrap:wrap; gap:8px; padding:4px 10px 10px 12px; }
.spacer { flex:1; }
.hint { font-size:12px; color:var(--muted); }
.iconbtn { width:36px; height:36px; display:grid; place-items:center; padding:0; border:1px solid var(--line); border-radius:50%; background:transparent; color:var(--ink-2); cursor:pointer; transition:background-color .15s, color .15s; }
.iconbtn:hover { background:var(--hover); color:var(--ink); }
#send { width:36px; height:36px; display:grid; place-items:center; padding:0; border:0; border-radius:50%; background:var(--accent); color:var(--accent-ink); cursor:pointer; transition:transform .15s, opacity .15s; }
#send:hover { transform:scale(1.06); }
#send:disabled { opacity:.25; cursor:default; transform:none; }
#chiprow { display:flex; flex-wrap:wrap; gap:6px; }
.chip { display:inline-flex; align-items:center; gap:4px; padding:3px 4px 3px 10px; border:1px solid var(--line); border-radius:999px; background:var(--hover); font-size:12px; }
.chip button { display:grid; place-items:center; padding:2px; border:0; border-radius:50%; background:transparent; color:var(--muted); cursor:pointer; }
.chip button:hover { color:var(--ink); background:var(--panel); }

#backdrop { display:none; }
@media (max-width: 720px) {
  aside { position:fixed; top:0; bottom:0; left:0; z-index:20; width:min(300px, 86vw); transform:translateX(-100%); transition:transform .2s; }
  aside.open { transform:none; box-shadow:4px 0 24px rgba(0,0,0,.25); }
  #backdrop.show { display:block; position:fixed; inset:0; z-index:10; background:rgba(0,0,0,.4); }
  #menuBtn { display:inline-flex; }
  #convbar { padding:0 12px; }
  #conn { padding:8px 12px; }
  #stream { padding:16px 12px 8px; }
  #composer { padding:8px 10px 12px; }
  .pagebar { padding:8px 12px; }
  #hiddenList { padding:14px 12px; }
  .hcard { flex-wrap:wrap; }
  .acts { width:100%; justify-content:flex-end; }
  .msg .body { max-width:85%; }
  .file { min-width:0; }
  .hint { display:none; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation:none !important; transition:none !important; }
}
/* ---- 群聊：沿用主题、机器人与唤醒状态 ---- */
.groups-head { justify-content:space-between; padding-bottom:10px; }
.new-group { display:flex; align-items:center; gap:3px; border:0; background:none; padding:3px; color:var(--ink-2); font-size:12px; cursor:pointer; }
.new-group:hover { color:var(--accent); }
.group-row { display:block; width:100%; text-align:left; border:1px solid transparent; background:none; border-radius:13px; padding:11px 10px; margin:3px 0; cursor:pointer; transition:background .15s, border-color .15s; }
.group-row:hover { background:var(--hover); }
.group-row.active { background:var(--accent-soft); border-color:var(--line); }
.group-line { display:flex; align-items:center; gap:7px; min-width:0; }
.group-name { flex:1; min-width:0; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.group-time { color:var(--muted); font-size:11px; flex-shrink:0; }
.group-dot { width:6px; height:6px; border:1px solid var(--muted); border-radius:50%; flex-shrink:0; }
.group-dot.online { background:var(--ok); border-color:var(--ok); box-shadow:0 0 6px color-mix(in srgb, var(--ok), transparent 60%); }
.group-preview { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--muted); font-size:12px; }
.group-row .group-line + .group-line { margin-top:9px; }
.group-count { flex-shrink:0; min-width:19px; height:19px; padding:0 5px; display:grid; place-items:center; border-radius:7px; color:var(--accent-ink); background:var(--accent); font-size:10px; font-weight:700; }
.group-stack { display:flex; align-items:center; padding-left:4px; flex-shrink:0; }
.group-stack .avatar { width:22px; height:22px; margin-left:-4px; border-radius:50%; background:var(--panel); }
.group-stack .more { padding-left:2px; font-size:10px; color:var(--muted); }
.group-empty { padding:6px 10px 12px; color:var(--muted); font-size:12px; }
#groupHeader { flex-shrink:0; padding:0 24px 14px; background:var(--panel); border-bottom:1px solid var(--line); }
#groupAbout { font-size:12px; color:var(--ink-2); margin:8px 0 12px; }
#groupAbout summary { cursor:pointer; color:var(--muted); }
#groupAbout p { margin:8px 0; white-space:pre-wrap; overflow-wrap:anywhere; max-height:150px; overflow-y:auto; }
#groupMembers { display:flex; align-items:center; flex-wrap:wrap; gap:7px; }
.member-chip { display:flex; align-items:center; gap:6px; padding:5px 10px 5px 6px; border:1px solid var(--line); border-radius:10px; background:var(--bg); font-size:12px; cursor:pointer; max-width:100%; }
.member-chip:hover { background:var(--hover); border-color:var(--muted); }
.member-chip .avatar { width:25px; height:25px; }
.member-chip .member-name { overflow:hidden; text-overflow:ellipsis; }
.member-chip .wake-dot { position:static; }
.group-label { border:1px solid var(--line); border-radius:5px; padding:1px 5px; font-size:10px; color:var(--ink-2); background:var(--hover); cursor:pointer; max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
body.in-group .group-label { display:none; }
body.in-group #convbar { border-bottom:0; }
main.page > #groupHeader { display:none; }
.group-form label.field { display:flex; flex-direction:column; gap:6px; margin-bottom:14px; font-size:12px; color:var(--ink-2); }
.group-form .tin { width:100%; font-size:14px; }
.group-form textarea { resize:vertical; min-height:80px; max-height:200px; }
.group-form .pick-row .info { flex:1; min-width:0; }
.group-form .pick-row .name { display:block; overflow:hidden; text-overflow:ellipsis; }
.group-form .pick-row .tsub { margin:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.group-actions { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-top:16px; }
.group-actions .bigbtn { width:auto; margin:0; padding:9px 24px; }
.group-event { margin:4px 0 22px; text-align:center; color:var(--muted); font-size:11px; overflow-wrap:anywhere; }
.group-event time { margin-left:7px; opacity:.75; }
.box { position:relative; }
#mentions { position:absolute; bottom:calc(100% + 9px); left:0; right:0; max-height:260px; overflow-y:auto; padding:6px; border:1px solid var(--line); border-radius:14px; background:var(--panel); box-shadow:0 12px 40px rgba(0,0,0,.2); z-index:5; }
.mention-option { display:flex; align-items:center; gap:9px; width:100%; padding:8px 10px; border:0; border-radius:9px; background:none; text-align:left; cursor:pointer; }
.mention-option.active { background:var(--accent-soft); }
.mention-option .avatar { width:28px; height:28px; }
.mention-option .info { flex:1; min-width:0; }
.mention-option .name { font-size:13px; }
.mention-option .tsub { margin:0; font-size:11px; }
.mention-option .invite { color:var(--muted); font-size:11px; }
#groupOptions { font-size:22px; line-height:1; letter-spacing:1px; }
@media (max-width:700px) {
  #groupHeader { padding:0 12px 12px; }
  #groupMembers { flex-wrap:nowrap; overflow-x:auto; padding:2px 0 5px; }
  .member-chip { flex-shrink:0; max-width:210px; }
  body.in-group #showAll { font-size:11px; padding:5px 7px; }
  #groupAbout p { max-height:100px; }
}
.backup-fields { display:grid; grid-template-columns:1fr 1fr; gap:10px 12px; margin:12px 0; }
.backup-field { display:flex; flex-direction:column; gap:5px; min-width:0; font-size:12px; color:var(--ink-2); }
.backup-field.wide { grid-column:1 / -1; }
.backup-field .tin { width:100%; min-width:0; box-sizing:border-box; }
.backup-settings summary { cursor:pointer; font-weight:600; font-size:14px; }
.backup-warning { padding:9px 12px; border-radius:9px; background:rgba(245,158,11,.12); color:var(--ink); font-size:12px; line-height:1.7; }
.backup-progress { width:100%; height:6px; accent-color:var(--accent); margin:10px 0 4px; }
.backup-status { font-size:12px; line-height:1.7; overflow-wrap:anywhere; color:var(--muted); }
.backup-status.failed { color:#e5484d; }
.backup-row { display:flex; gap:12px; align-items:center; padding:12px 0; border-bottom:1px solid var(--line); }
.backup-info { flex:1; min-width:0; overflow-wrap:anywhere; }
.backup-info strong { font-size:13px; font-weight:600; }
.backup-actions { display:flex; gap:6px; }
.backup-panel h3 { display:flex; align-items:center; justify-content:space-between; gap:12px; }
.backup-confirm { margin-top:12px; padding:12px; border:1px solid var(--line); border-radius:10px; }
.backup-confirm .trow { margin-top:10px; }
.backup-check { display:flex; align-items:center; gap:8px; font-size:13px; }
.backup-check input { accent-color:var(--accent); }
#restoreBanner { flex-shrink:0; padding:10px 20px; background:rgba(245,158,11,.16); color:var(--ink); font-size:13px; border-bottom:1px solid rgba(245,158,11,.3); }
@media(max-width:500px) {
  .backup-fields { grid-template-columns:1fr; }
  .backup-row { flex-wrap:wrap; gap:8px; }
  .backup-actions { margin-left:auto; }
}
</style>
</head>
<body>
<aside id="side">
  <div class="brand"><span class="mark" data-logo></span><div><div class="logo">kopitiam</div><div class="sub" id="brandsub">本地聊天室</div></div></div>
  <div id="sideScroll"><div id="groups"></div><div id="agents"></div></div>
  <div id="themes">
    <button id="backupBtn" class="sidebtn"><span data-icon="cloud"></span>云备份<span class="cur" id="backupCur">未设置</span></button>
    <button id="wakeBtn" class="sidebtn"><span data-icon="bolt"></span>自动唤醒<span class="cur" id="wakeCur"></span></button>
    <button id="themeBtn" class="sidebtn" aria-haspopup="true"><span data-icon="palette"></span>主题<span class="cur" id="themeCur"></span></button>
    <div id="themePop" hidden></div>
  </div>
</aside>
<div id="backdrop"></div>
<div id="modal" hidden>
  <div class="mcard" role="dialog" aria-modal="true">
    <div class="mhead"><span id="mtitle"></span><button id="mclose" class="iconbtn" aria-label="关闭"><span data-icon="x"></span></button></div>
    <div id="mbody"></div>
  </div>
</div>
<main id="main">
  <div id="convbar">
    <button id="menuBtn" class="btn" aria-label="成员列表"><span data-icon="menu"></span></button>
    <span id="convAvatar" hidden></span>
    <div class="titles"><div id="convtitle">全部消息</div><div id="convsub">所有人之间的消息都在这里</div></div>
    <button id="showAll" class="btn" hidden>显示全部</button>
    <button id="groupOptions" class="iconbtn" aria-label="群聊设置" title="群聊设置" hidden>⋯</button>
  </div>
  <div id="groupHeader" hidden>
    <details id="groupAbout"><summary>群聊说明</summary><p></p></details>
    <div id="groupMembers"></div>
  </div>
  <div id="conn" role="status" hidden></div>
  <div id="restoreBanner" role="status" hidden>正在恢复 / 已恢复，请关掉 kopitiam web 再重新打开，再重开各个 agent。</div>
  <div id="stream">
    <div id="list"></div>
    <div id="groupList" hidden></div>
    <div id="empty"><span id="emptyArt"><span class="mark" data-logo data-size="34"></span></span><div id="emptyTitle">kopitiam</div><div id="emptySub">还没有消息</div></div>
  </div>
  <div id="composer">
    <div class="box">
      <div id="mentions" role="listbox" aria-label="点名成员" hidden></div>
      <div class="torow"><span class="lbl">发给</span><input id="to" placeholder="所有人（多个名字用逗号分隔）"></div>
      <textarea id="text" aria-label="消息" placeholder="写点什么…" aria-controls="mentions" aria-expanded="false"></textarea>
      <div class="tools">
        <input id="file" type="file" multiple hidden>
        <button id="pick" class="iconbtn" title="添加附件" aria-label="添加附件"><span data-icon="clip"></span></button>
        <span id="chiprow"></span>
        <span class="spacer"></span>
        <span class="hint">Ctrl+Enter 发送</span>
        <button id="send" title="发送（Ctrl+Enter）" aria-label="发送" disabled><span data-icon="send"></span></button>
      </div>
    </div>
  </div>
  <section id="hiddenPage" hidden>
    <div class="pagebar">
      <button id="hiddenBack" class="btn"><span data-icon="back"></span>返回</button>
      <div class="titles"><div class="ptitle">已归档 / 隐藏的 agent</div><div class="psub">你归档的、离线超过 7 天的都收在这里。归档的不会被叫醒，它自己再用 kopitiam 就会回到列表；有消息的可以移到回收站，30 天后连同消息一起删除</div></div>
    </div>
    <div id="hiddenList"></div>
  </section>
</main>
<script>
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);
let lastId = 0, pendingFiles = [], agentJson = "", offlineOpen = false, booted = false;
let selected = null;  // 当前对话的 agent 名字；null = 显示全部消息
let selectedGroup = null, groupsById = {}, groupJson = "", groupHeadJson = "", groupHistoryKey = "", groupHistorySeq = 0;
let archivedGroupsOpen = false;
const seenGroups = {}, loadedGroups = {};
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

// ---- 图标：线条 SVG。innerHTML 只用于下面这些写死的常量，不碰任何消息内容 ----
const ICONS = {
  cloud: '<path d="M6 17.5a4 4 0 0 1-.6-8 6.5 6.5 0 0 1 12.5 1.7 3.2 3.2 0 1 1 .1 6.3"/><path d="M12 20V11m-3 3 3-3 3 3"/>',
  clip: '<path d="M20.5 11.5l-8.2 8.2a5 5 0 0 1-7.1-7.1l8.6-8.6a3.4 3.4 0 0 1 4.8 4.8l-8.6 8.6a1.7 1.7 0 0 1-2.4-2.4l7.9-7.9"/>',
  send: '<path d="M12 19V5"/><path d="M5.5 11.5L12 5l6.5 6.5"/>',
  menu: '<path d="M4 7h16"/><path d="M4 12h16"/><path d="M4 17h10"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6"/><path d="M9 17h4"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  x: '<path d="M7 7l10 10"/><path d="M17 7L7 17"/>',
  chev: '<path d="M9.5 6l6 6-6 6"/>',
  back: '<path d="M14.5 6l-6 6 6 6"/>',
  restore: '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3"/><path d="M4.5 4.5v4h4"/>',
  open: '<path d="M14 4.5h5.5V10"/><path d="M19.5 4.5L11 13"/><path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/>',
  folder: '<path d="M3.5 7A1.5 1.5 0 0 1 5 5.5h4.3l2 2.2H19a1.5 1.5 0 0 1 1.5 1.5V17A1.5 1.5 0 0 1 19 18.5H5A1.5 1.5 0 0 1 3.5 17z"/>',
  down: '<path d="M7 10l5 5 5-5"/>',
  archive: '<rect x="3.5" y="4" width="17" height="4.5" rx="1.5"/><path d="M5.5 8.5V18a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8.5"/><path d="M10 12.5h4"/>',
  trash: '<path d="M4 7h16"/><path d="M9 7V4.5h6V7"/><path d="M6.5 7l1 12.5a1.5 1.5 0 0 0 1.5 1.5h6a1.5 1.5 0 0 0 1.5-1.5l1-12.5"/>',
  chat: '<path d="M20 12a8 8 0 0 1-11.6 7.1L4 20l1-4.2A8 8 0 1 1 20 12z"/>',
  fwd: '<path d="M14 5l6 6-6 6"/><path d="M20 11H10a6 6 0 0 0-6 6v2"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  palette: '<path d="M12 3a9 9 0 1 0 0 18c1.1 0 1.8-.8 1.8-1.8 0-.5-.2-.9-.5-1.2-.3-.3-.5-.7-.5-1.2 0-1 .8-1.8 1.8-1.8H17a4 4 0 0 0 4-4C21 6.6 17 3 12 3z"/><circle cx="7.5" cy="11.5" r="1"/><circle cx="10" cy="7.5" r="1"/><circle cx="14.5" cy="7.5" r="1"/>',
  bolt: '<path d="M13 3L5 13.5h6L10 21l8-10.5h-6z"/>',
};
const LOGO = '<path d="M6 13h17v6a7 7 0 0 1-7 7h-3a7 7 0 0 1-7-7z"/><path d="M23 15h1.5a3.5 3.5 0 0 1 0 7H22.4"/><path d="M11.5 9.5c0-1.6 1.6-1.6 1.6-3.2s-1.6-1.6-1.6-3.2"/><path d="M17 9.5c0-1.6 1.6-1.6 1.6-3.2S17 4.7 17 3.1"/>';

function svg(paths, size, view, width) {
  return '<svg viewBox="0 0 ' + view + ' ' + view + '" width="' + size + '" height="' + size +
    '" fill="none" stroke="currentColor" stroke-width="' + width + '" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + paths + '</svg>';
}

function icon(name, size) {
  const s = el("span", "ic");
  s.innerHTML = svg(ICONS[name], size || 18, 24, 1.8);
  return s;
}

function hue(name) {
  let h = 0;
  for (const c of name) h = (h * 31 + c.codePointAt(0)) % 360;
  return h;
}

function shown(name) { return name === "user" ? "你" : name; }

function seed(name) {  // FNV-1a：和 hue 独立的第二个哈希，用来挑机器人的部件和动画节奏
  let x = 2166136261;
  for (const c of name) { x ^= c.codePointAt(0); x = Math.imul(x, 16777619) >>> 0; }
  return x;
}

// 机器人部件（写死的 SVG 常量；名字只用来算编号，不会进 innerHTML）
const BOT = {
  ant: [
    '<line class="stalk" x1="20" y1="10" x2="20" y2="5"/><circle class="light" cx="20" cy="3.6" r="2.4"/>',
    '<line class="stalk" x1="14.5" y1="10" x2="12" y2="5.2"/><line class="stalk" x1="25.5" y1="10" x2="28" y2="5.2"/>' +
      '<circle class="light" cx="11.6" cy="4.4" r="2"/><circle class="light" cx="28.4" cy="4.4" r="2"/>',
    '<rect class="light" x="14.5" y="6.2" width="11" height="3.8" rx="1.9"/>',
  ],
  eye: [
    '<circle class="eye" cx="15.5" cy="20.5" r="2.3"/><circle class="eye" cx="24.5" cy="20.5" r="2.3"/>',
    '<rect class="eye" x="14" y="17.8" width="3" height="5.6" rx="1.5"/><rect class="eye" x="23" y="17.8" width="3" height="5.6" rx="1.5"/>',
    '<rect class="eye" x="13" y="19" width="14" height="3.4" rx="1.7"/>',
  ],
  sleep: ["M13.5 21h4M22.5 21h4", "M13.5 21h4M22.5 21h4", "M14 21h12"],
  mouth: [
    '<rect class="mouth" x="19" y="24.4" width="2" height="1.3" rx=".65"/>',
    '<rect class="mouth" x="17.3" y="24.4" width="5.4" height="1.3" rx=".65"/>',
    '<path class="mouth s" d="M17.2 24c1.6 1.5 4 1.5 5.6 0"/>',
  ],
};

function bot(name, online, still) {
  const v = seed(name);
  const ant = v % 3, eye = Math.floor(v / 3) % 3, mouth = Math.floor(v / 9) % 3;
  const a = el("div", "avatar bot " + (online ? "on" : "off") + (still ? " still" : ""));
  a.style.setProperty("--h", hue(name));
  // 负的延迟 = 动画一开始就处在不同进度，几个机器人不会同时眨眼
  a.style.setProperty("--d", -((v >>> 8) % 50) / 10 + "s");
  a.style.setProperty("--d2", -((v >>> 16) % 90) / 10 + "s");
  a.innerHTML = '<svg viewBox="1 -0.5 38 35" aria-hidden="true"><g class="face">' + BOT.ant[ant] +
    '<rect class="ear" x="3" y="18" width="3.5" height="8" rx="1.5"/><rect class="ear" x="33.5" y="18" width="3.5" height="8" rx="1.5"/>' +
    '<rect class="head" x="6" y="10" width="28" height="23" rx="8"/>' +
    '<rect class="visor" x="9.5" y="14.5" width="21" height="13" rx="5"/>' +
    '<g class="eyes"><g class="lids">' + BOT.eye[eye] + '</g></g>' +
    '<path class="sleep" d="' + BOT.sleep[eye] + '"/>' + BOT.mouth[mouth] + '</g></svg>';
  if (!online && !still) a.append(el("span", "z", "z"));
  return a;
}

function human(name) {  // 人：圆脸小人（呆毛、腮红、微笑），和机器人一眼就能分开；同事的发色按名字变
  const a = el("div", "avatar human" + (name ? " peer" : ""));
  if (name) a.style.setProperty("--hh", hue(name));
  a.innerHTML = '<svg viewBox="1 -0.5 38 35" aria-hidden="true"><g class="face">' +
    '<path class="strand" d="M20.5 7.6c-.5-2.6.8-4.6 3.3-5.2"/>' +
    '<circle class="skin" cx="7.8" cy="20.6" r="2.2"/><circle class="skin" cx="32.2" cy="20.6" r="2.2"/>' +
    '<circle class="skin" cx="20" cy="19.6" r="12.4"/>' +
    '<path class="hair" d="M7.6 18.6C7.6 11.3 13 7.1 20 7.1s12.4 4.2 12.4 11.5c-1.6-2.4-4-4-7-4.6-1.8 1.7-5.6 2.9-9.6 2.9-2.9 0-5.4-.6-7-1.6-.5.6-.9 1.4-1.2 2.3z"/>' +
    '<g class="lids"><ellipse class="eyeh" cx="15.6" cy="21" rx="1.5" ry="1.9"/><ellipse class="eyeh" cx="24.4" cy="21" rx="1.5" ry="1.9"/></g>' +
    '<circle class="cheek" cx="12.8" cy="24.6" r="2.1"/><circle class="cheek" cx="27.2" cy="24.6" r="2.1"/>' +
    '<path class="smile" d="M17.6 25.2c1.4 1.3 3.4 1.3 4.8 0"/></g></svg>';
  return a;
}

function avatar(name, online, still, kind) {
  if (name === "user") return human();
  if ((kind || (agentsByName[name] || {}).kind) === "remote") return human(name);  // 别的电脑上的同事
  return bot(name, online, still);
}

function kindLabel(k) { return k === "remote" ? "同事" : k; }

function post(path, data) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
}

// ---- 弹窗（团队设置、转发共用） ----
let modalCleanup = null;
function openModal(title, body, onClose) {
  closeModal();
  $("mtitle").textContent = title;
  $("mbody").replaceChildren(body);
  $("modal").hidden = false;
  modalCleanup = onClose || null;
}
function closeModal() {
  if (modalCleanup) { modalCleanup(); modalCleanup = null; }
  $("modal").hidden = true;
  $("mbody").textContent = "";
}

function tsec(title, sub) {
  const s = el("div", "tsec");
  s.append(el("h3", "", title));
  if (sub) s.append(el("div", "tsub", sub));
  return s;
}
function trow(...kids) { const r = el("div", "trow"); r.append(...kids); return r; }
function flash(where, out, okText) {
  where.querySelector(".mok, .merr")?.remove();
  where.append(el("div", out.ok ? "mok" : "merr", out.message || (out.ok ? okText || "好了" : "失败")));
}

// ---- 云备份：密码只输入，从不从服务端取回 ----
let backupInfo = null, backupView = null, backupPolling = false, restartRequired = false;
function applyRestoreState(required) {
  if (!required) return;
  restartRequired = true;
  $("restoreBanner").hidden = false;
  for (const id of ["text", "to", "pick", "file", "send"]) $(id).disabled = true;
  hideMentions();
}
function backupAge(iso) {
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(iso)) / 60000));
  return minutes < 1 ? "刚刚" : minutes < 60 ? minutes + " 分钟前" : minutes < 1440 ? Math.floor(minutes / 60) + " 小时前" : Math.floor(minutes / 1440) + " 天前";
}
async function pollBackup() {
  if (backupPolling) return;
  backupPolling = true;
  try {
    const state = await api("/api/backup");
    if (!state.config) return;
    backupInfo = state;
    applyRestoreState(state.restart_required);
    const j = state.job, running = j.state === "running";
    $("backupCur").textContent = running ? (j.kind === "backup" ? "备份中 " : "恢复中 ") + j.progress + "%"
      : j.state === "failed" ? "操作失败" : state.last_backup ? backupAge(state.last_backup.time) : state.config.has_password ? "尚未备份" : "未设置";
    if (backupView) backupView(state);
  } catch (e) { $("backupCur").textContent = "连接中断"; }
  finally { backupPolling = false; }
}
function backupNotice(where, result, fallback="操作完成") {
  where.textContent = result.message || result.error || (result.ok ? fallback : "操作失败");
  where.className = result.ok ? "mok" : "merr";
}
async function openBackup() {
  closeSide();
  const body = el("div", "backup-panel"), loading = el("div", "mnote", "正在读取备份设置…");
  body.append(loading);
  openModal("云备份", body, () => { backupView = null; });
  const state = await api("/api/backup").catch(() => ({message:"连不上 kopitiam web"}));
  if (!body.isConnected) return;
  if (!state.config) { loading.textContent = state.message || "无法读取设置"; return; }
  body.replaceChildren();
  const config = state.config, settings = el("details", "tsec backup-settings");
  settings.open = !config.has_password || !config.has_backup_password;
  settings.append(el("summary", "", "WebDAV 设置" + (config.has_password ? " · 已设置" : "")));
  const grid = el("div", "backup-fields"), inputs = {};
  function field(key, label, value, placeholder, secret=false, wide=false) {
    const wrap = el("label", "backup-field" + (wide ? " wide" : "")), input = el("input", "tin");
    input.type = secret ? "password" : "text";
    input.value = value || ""; input.placeholder = placeholder || "";
    input.autocomplete = secret ? "new-password" : "off";
    input.spellcheck = false;
    wrap.append(el("span", "", label), input); grid.append(wrap); inputs[key] = input;
  }
  field("url", "WebDAV 地址", config.url, "https://dav.jianguoyun.com/dav/", false, true);
  field("user", "用户名", config.user, "你的账号 / 邮箱");
  field("folder", "远程文件夹", config.folder, "kopitiam");
  field("password", "应用密码", "", config.has_password ? "已设置 · 输入新密码修改" : "云盘的应用密码", true, true);
  field("backup_password", "备份密码", "", config.has_backup_password ? "已设置 · 输入新密码修改" : "用于加密备份", true);
  field("confirmation", "确认备份密码", "", "修改时再输入一次", true);
  const warning = el("div", "backup-warning", "忘了备份密码，备份就恢复不了。请把它保存在可靠的地方；在另一台电脑恢复时也需要它。");
  const save = el("button", "btn", "保存设置"), test = el("button", "btn", "测试连接"), settingNote = el("div");
  const settingControls = trow(save, test);
  settings.append(grid, warning, settingControls, settingNote);
  settingControls.style.marginTop = "12px";
  save.onclick = async () => {
    if (inputs.backup_password.value !== inputs.confirmation.value) return backupNotice(settingNote, {message:"两次备份密码不一致"});
    save.disabled = true;
    try {
      const values = Object.fromEntries(Object.entries(inputs).filter(([key]) => key !== "confirmation").map(([key, input]) => [key, input.value]));
      const out = await post("/api/backup/config", values);
      backupNotice(settingNote, out, "设置已保存，密码已在本机加密保存");
      if (out.ok) {
        for (const key of ["password", "backup_password", "confirmation"]) inputs[key].value = "";
        for (const key of ["password", "backup_password"]) if (out.config["has_" + key]) inputs[key].placeholder = "已设置 · 输入新密码修改";
        settings.querySelector("summary").textContent = "WebDAV 设置 · 已设置";
        await pollBackup(); await refreshList();
      }
    } catch (e) { backupNotice(settingNote, {message:"保存失败，请检查连接"}); }
    finally { save.disabled = false; }
  };
  test.onclick = async () => {
    test.disabled = true;
    try { backupNotice(settingNote, await post("/api/backup/test", {})); }
    catch (e) { backupNotice(settingNote, {message:"测试失败，请检查连接"}); }
    finally { test.disabled = false; }
  };
  const runSection = tsec("立即备份", "加密后上传到你的云盘；每台电脑保留最近 10 份。"), check = el("input");
  check.type = "checkbox"; check.checked = false;  // 附件可能很大，默认只备份聊天记录
  const size = el("span"), label = el("label", "backup-check"); label.append(check, size);
  const run = el("button", "bigbtn", "加密并备份"), progress = el("progress", "backup-progress"), status = el("div", "backup-status");
  progress.max = 100; progress.value = 0; status.setAttribute("role", "status");
  const runNote = el("div"), retryBox = el("div", "backup-confirm"); retryBox.hidden = true;
  const retryPassword = el("input", "tin"); retryPassword.type = "password"; retryPassword.placeholder = "这份备份的密码"; retryPassword.setAttribute("aria-label", "这份备份的密码");
  const retry = el("button", "btn", "用此密码重试");
  retryBox.append(el("div", "mnote", "密码不对或文件损坏。来自另一台电脑的备份，请输入创建它时使用的备份密码。"), trow(retryPassword, retry));
  async function startRestore(name, password) {
    const out = await post("/api/backup/restore", {name, ...(password ? {backup_password:password} : {})});
    backupNotice(runNote, out, "恢复任务已开始");
    if (out.ok) retryPassword.value = "";
    await pollBackup();
  }
  retry.onclick = async () => {
    if (!retryPassword.value) return retryPassword.focus();
    retry.disabled = true;
    try { await startRestore(backupInfo?.job.name || state.job.name, retryPassword.value); }
    catch (e) { backupNotice(runNote, {message:"无法开始恢复，请检查连接"}); }
    finally { retry.disabled = false; }
  };
  run.onclick = async () => {
    run.disabled = true;
    try { backupNotice(runNote, await post("/api/backup/run", {files:check.checked}), "备份任务已开始"); await pollBackup(); }
    catch (e) { backupNotice(runNote, {message:"无法开始备份，请检查连接"}); run.disabled = false; }
  };
  runSection.append(label, run, progress, status, retryBox, runNote);
  const listSection = tsec("备份列表"), list = el("div"), listNote = el("div"), confirmation = el("div", "backup-confirm");
  confirmation.hidden = true;
  const refresh = el("button", "btn", "刷新列表"); listSection.querySelector("h3").append(refresh);
  function confirmAction(entry, remove) {
    confirmation.hidden = false; confirmation.replaceChildren();
    confirmation.append(el("div", "mnote", remove ? "永久删除这份云端备份：" + entry.name + "？" :
      "恢复将覆盖现在的聊天记录。同名且内容不同的附件会先连同现有数据库保存在本机副本目录。请先关闭各个 agent；恢复后要关闭并重新打开 kopitiam web，再重开 agent。"));
    const go = el("button", "btn" + (remove ? " danger" : ""), remove ? "确认删除" : "确认恢复"), cancel = el("button", "btn", "取消");
    cancel.onclick = () => { confirmation.hidden = true; };
    go.onclick = async () => {
      go.disabled = true;
      try {
        if (remove) { backupNotice(listNote, await post("/api/backup/delete", {name:entry.name})); await refreshList(); }
        else await startRestore(entry.name);
        confirmation.hidden = true;
      } catch (e) { backupNotice(listNote, {message:"操作失败，请检查连接"}); }
      finally { go.disabled = false; }
    };
    confirmation.append(trow(go, cancel)); confirmation.scrollIntoView({block:"nearest"});
  }
  async function refreshList() {
    refresh.disabled = true;
    try {
      const entries = await api("/api/backup/list");
      if (!body.isConnected) return;
      list.replaceChildren();
      if (!Array.isArray(entries)) { list.append(el("div", "mnote", entries.message || "请先保存 WebDAV 设置")); return; }
      if (!entries.length) list.append(el("div", "mnote", "还没有云端备份。完成设置后，点上面的「加密并备份」。"));
      for (const item of entries) {
        const row = el("div", "backup-row"), info = el("div", "backup-info"), actions = el("div", "backup-actions");
        info.append(el("strong", "", item.time ? item.time.replace("T", " ") : "时间未知"), el("div", "mnote", item.host + " · " + fmtSize(item.size)));
        info.title = item.name;
        for (const remove of [false, true]) {
          const button = el("button", "btn" + (remove ? " danger" : ""), remove ? "删除" : "恢复");
          button.disabled = backupInfo?.job.state === "running";
          button.onclick = () => confirmAction(item, remove); actions.append(button);
        }
        row.append(info, actions); list.append(row);
      }
    } catch (e) { backupNotice(listNote, {message:"读取列表失败，请检查连接"}); }
    finally { refresh.disabled = false; }
  }
  refresh.onclick = refreshList;
  listSection.append(list, confirmation, listNote);
  body.append(settings, runSection, listSection);
  let previousJob = "";
  backupView = s => {
    const j = s.job, running = j.state === "running";
    if (!running && runNote.className === "mok") runNote.textContent = "";
    size.textContent = "包含附件 · " + fmtSize(s.files_bytes);
    run.disabled = running || s.restart_required || !s.config.has_password || !s.config.has_backup_password;
    check.disabled = running;
    progress.hidden = j.state === "idle"; progress.value = j.progress;
    status.className = "backup-status" + (j.state === "failed" ? " failed" : "");
    status.textContent = running ? j.step + (j.progress ? " · " + j.progress + "%" : "…") : j.message || "备份由你手动触发，不会自动上传。";
    retryBox.hidden = !(j.state === "failed" && j.needs_password);
    for (const button of list.querySelectorAll("button")) button.disabled = running;
    const key = j.started_at + j.state;
    if (j.state === "done" && previousJob && previousJob !== key) refreshList();
    previousJob = key;
  };
  backupInfo = state; backupView(state); applyRestoreState(state.restart_required);
  await refreshList();
}

// ---- 群聊 ----
function groupStack(g) {
  const stack = el("span", "group-stack"), members = g.members.filter(n => n !== "user");
  for (const name of members.slice(0, 3)) stack.append(avatar(name, !!agentsByName[name]?.online));
  if (!members.length) stack.append(avatar("user", true));
  if (members.length > 3) stack.append(el("span", "more", "+" + (members.length - 3)));
  return stack;
}

function groupRow(g) {
  const row = el("button", "group-row" + (selectedGroup === g.id ? " active" : ""));
  row.setAttribute("aria-label", "打开群聊 " + g.title);
  const line = el("div", "group-line");
  const online = g.members.some(n => n !== "user" && agentsByName[n]?.online);
  const dot = el("span", "group-dot" + (online ? " online" : ""));
  dot.title = online ? "有成员在线" : "成员离线";
  line.append(dot, el("span", "group-name", g.title), el("span", "group-time", fmtTime(g.last?.time || g.created_at)));
  const sub = el("div", "group-line");
  sub.append(groupStack(g), el("span", "group-preview", g.last ? shown(g.last.sender) + "：" + (g.last.preview || "附件") : "还没有消息"));
  if (g.unread) sub.append(el("span", "group-count", g.unread > 99 ? "99+" : String(g.unread)));
  row.append(line, sub);
  row.onclick = () => selectGroup(g.id);
  return row;
}

function renderGroups() {
  const groups = Object.values(groupsById).sort((a, b) => (b.last?.time || b.created_at).localeCompare(a.last?.time || a.created_at)
    || (b.last?.id || 0) - (a.last?.id || 0) || b.id - a.id);
  const key = JSON.stringify(groups) + selectedGroup + JSON.stringify(Object.values(agentsByName).map(a => [a.name, a.online]));
  if (key === groupJson) return;
  groupJson = key;
  const box = $("groups"), head = el("div", "section groups-head"), create = el("button", "new-group", "新群聊");
  create.prepend(icon("plus", 13)); create.onclick = () => openGroupEditor();
  head.append(el("span", "", "群聊"), create);
  box.replaceChildren(head);
  const active = groups.filter(g => !g.archived_at), archived = groups.filter(g => g.archived_at);
  for (const g of active) box.append(groupRow(g));
  if (!active.length) box.append(el("div", "group-empty", "把一起做事的 agent 放进一个群"));
  if (archived.length) {
    const det = el("details", "offline"); det.open = archivedGroupsOpen;
    det.ontoggle = () => { archivedGroupsOpen = det.open; };
    const sum = el("summary", "section"); sum.append(icon("chev", 12), document.createTextNode("已归档 · " + archived.length));
    det.append(sum, ...archived.map(groupRow)); box.append(det);
  }
  // 群改名以后，已经在「全部消息」里的标签也跟着更新。
  for (const label of document.querySelectorAll(".group-label")) {
    label.textContent = "群聊·" + (groupsById[label.dataset.group]?.title || label.dataset.group);
  }
}

function renderGroupHeader() {
  const g = groupsById[selectedGroup];
  if (!g) return;
  const key = JSON.stringify(g) + JSON.stringify(wakeInfo) + JSON.stringify(g.members.map(n => agentsByName[n]));
  if (key === groupHeadJson) return;
  groupHeadJson = key;
  $("convtitle").textContent = g.title;
  $("convsub").textContent = "群聊 " + g.id + " · " + g.members.length + " 位成员" + (g.archived_at ? " · 已归档" : "");
  $("convAvatar").hidden = true;
  $("showAll").hidden = false;
  $("groupOptions").hidden = false;
  $("groupHeader").hidden = false;
  $("groupAbout").querySelector("summary").textContent = g.about ? "说明 · " + g.about.split("\\n")[0].slice(0, 48) : "添加群聊说明";
  $("groupAbout").querySelector("p").textContent = g.about || "在群聊设置里写下目标，让新加入的成员快速了解这件事。";
  const members = $("groupMembers"); members.replaceChildren();
  for (const name of g.members) {
    const chip = el("button", "member-chip"), a = agentsByName[name];
    chip.title = name + (a?.title ? " · " + a.title : "") + (name === "user" ? " · 用户" : a?.online ? " · 在线" : " · 离线");
    const av = avatar(name, name === "user" || !!a?.online);
    const bolt = wakeBadge(name);
    chip.append(av, el("span", "member-name", shown(name)));
    if (bolt) chip.append(bolt);
    chip.onclick = () => select(name);
    members.append(chip);
  }
  const add = el("button", "member-chip"); add.append(icon("plus", 15));
  add.title = "添加成员"; add.setAttribute("aria-label", "添加成员"); add.onclick = () => openGroupEditor(g, true);
  members.append(add);
  $("text").disabled = restartRequired || !!g.archived_at;
  $("pick").disabled = restartRequired || !!g.archived_at;
  $("text").placeholder = g.archived_at ? "这个群已归档，在群聊设置里恢复后继续" : "@ 点名，不 @ 就是发给全体成员";
  updateSend();
}

function groupCandidates(g) {
  return Object.values(agentsByName).filter(a => a.kind !== "remote" && a.name !== "user"
    && (g?.members.includes(a.name) || (!a.archived && !a.trashed)))
    .sort((a, b) => Number(b.online) - Number(a.online) || a.name.localeCompare(b.name));
}

function openGroupEditor(g = null, addOnly = false) {
  closeSide();
  const body = el("div", "team group-form");
  const title = el("input", "tin"), about = el("textarea", "tin");
  title.value = g?.title || ""; title.maxLength = 40; title.placeholder = "给这件事起个名字";
  about.value = g?.about || ""; about.maxLength = 2000; about.placeholder = "希望一起完成什么？（可选）";
  if (!addOnly) {
    for (const [label, input] of [["群聊名称", title], ["说明 / 目标", about]]) {
      const field = el("label", "field", label); field.append(input); body.append(field);
    }
  }
  body.append(el("div", "mlabel", addOnly ? "选择要拉进群的 agent" : "成员 · user 始终在群里"));
  const picker = el("div", "pick"), checks = [];
  for (const a of groupCandidates(g).filter(a => !addOnly || !g.members.includes(a.name))) {
    const row = el("label", "pick-row"), check = el("input"); check.type = "checkbox";
    check.checked = !addOnly && !!g?.members.includes(a.name);
    const info = el("div", "info");
    info.append(el("span", "name", a.name), el("div", "tsub", (a.online ? "在线" : "离线") + " · " + (a.title || a.kind)));
    row.append(check, avatar(a.name, a.online), info); picker.append(row); checks.push([a.name, check]);
  }
  if (!checks.length) picker.append(el("div", "mnote", "暂时没有可添加的 agent；可以先建群，之后再拉人。"));
  body.append(picker);
  const error = el("div", "merr"); error.setAttribute("role", "alert"); body.append(error);
  const actions = el("div", "group-actions"), save = el("button", "bigbtn", addOnly ? "添加成员" : g ? "保存更改" : "创建群聊");
  if (g && !addOnly) {
    const archive = opBtn(g.archived_at ? "restore" : "archive", g.archived_at ? "恢复群聊" : "归档群聊", "", async () => {
      try {
        const out = await post("/api/group", {id:g.id, archived:!g.archived_at});
        if (!out.ok) { error.textContent = out.message || "操作失败"; return; }
        closeModal(); await poll();
      } catch (e) { error.textContent = "连不上 kopitiam web，请重试"; }
    });
    actions.append(archive);
  } else actions.append(el("span"));
  actions.append(save); body.append(actions);
  save.onclick = async () => {
    const chosen = checks.filter(([, c]) => c.checked).map(([n]) => n);
    const data = g ? {id:g.id, add:chosen.filter(n => !g.members.includes(n))} : {title:title.value, about:about.value, members:chosen};
    if (g && !addOnly) Object.assign(data, {title:title.value, about:about.value, remove:g.members.filter(n => n !== "user" && !chosen.includes(n))});
    save.disabled = true;
    try {
      const out = await post(g ? "/api/group" : "/api/groups", data);
      if (!out.ok) { error.textContent = out.message || "保存失败"; return; }
      closeModal(); await poll(); selectGroup(out.group.id);
    } catch (e) { error.textContent = "连不上 kopitiam web，请重试"; }
    finally { save.disabled = false; }
  };
  openModal(addOnly ? "添加成员" : g ? "群聊设置" : "新群聊", body);
  if (!addOnly) title.focus();
}

function selectGroup(id) {
  if (!groupsById[id]) return;
  closeHidden(); closeSide(); hideMentions();
  selected = null; selectedGroup = id; groupHeadJson = ""; groupHistoryKey = "";
  document.body.classList.add("in-group");
  $("list").hidden = true; $("groupList").hidden = false; $("groupList").replaceChildren();
  $("to").closest(".torow").hidden = true;
  $("to").value = "";
  agentJson = ""; renderAgents(Object.values(agentsByName)); renderGroups(); renderGroupHeader();
  updateEmpty(); loadGroupHistory(id, true);
  if (!groupsById[id].archived_at) $("text").focus();
}

function groupEvent(e) {
  const actor = e.actor ? shown(e.actor) : "已删除的成员", agent = e.agent || "已删除的成员";
  const text = {created:actor + " 创建了群聊", added:actor + " 把 " + (e.agents || [agent]).join("、") + " 拉进了群聊",
    removed:actor + " 将 " + agent + " 移出了群聊", renamed:actor + " 将群聊改名为「" + e.detail + "」",
    updated:actor + " 更新了群聊说明", archived:actor + " 归档了群聊", restored:actor + " 恢复了群聊"}[e.kind] || "群聊已更新";
  const node = el("div", "group-event", text);
  node.append(el("time", "", fmtTime(e.time))); return node;
}

function timelineItems(messages, events) {
  const items = [...messages.map(m => ({time:m.time, id:m.id, data:m, event:false})),
    ...events.map(e => ({time:e.time, id:e.id, data:{...e}, event:true}))];
  // NOTE: 数据库时间精确到秒；同一秒内的事件排在消息前，保证排序稳定。
  items.sort((a, b) => a.time.localeCompare(b.time) || Number(b.event) - Number(a.event) || a.id - b.id);
  const result = [];
  for (const item of items) {
    const previous = result.at(-1);
    if (item.event && item.data.kind === "added") {
      const name = item.data.agent || "已删除的成员";
      if (previous?.event && previous.data.kind === "added" && item.data.actor && item.data.actor === previous.data.actor
          && item.time.slice(0, 16) === previous.time.slice(0, 16)) {
        previous.data.agents.push(name); continue;
      }
      item.data.agents = [name];
    }
    result.push(item);
  }
  return result;
}

async function loadGroupHistory(id, force = false) {
  const g = groupsById[id]; if (!g || selectedGroup !== id) return;
  const key = id + "|" + g.last?.id + "|" + g.count + "|" + g.event_id;
  if (!force && key === groupHistoryKey) { markGroupSeen(id, loadedGroups[id] || 0); return; }
  groupHistoryKey = key;
  const seq = ++groupHistorySeq;
  try {
    const out = await api("/api/messages?group=" + id);
    if (selectedGroup !== id || seq !== groupHistorySeq) return;
    if (!out.messages) { groupHistoryKey = ""; return; }
    const stream = $("stream"), bottom = force || stream.scrollTop + stream.clientHeight >= stream.scrollHeight - 40;
    const items = timelineItems(out.messages, out.events || []);
    $("groupList").replaceChildren(...items.map(x => x.event ? groupEvent(x.data) : messageNode(x.data)));
    updateEmpty(); if (bottom) stream.scrollTop = stream.scrollHeight;
    loadedGroups[id] = out.messages.at(-1)?.id || 0;
    markGroupSeen(id, loadedGroups[id]);
  } catch (e) { if (seq === groupHistorySeq) groupHistoryKey = ""; }
}

async function markGroupSeen(id, upto) {
  if (selectedGroup !== id || document.hidden || (seenGroups[id] || 0) >= upto) return;
  const previous = seenGroups[id] || 0; seenGroups[id] = upto;
  try {
    const out = await post("/api/seen", {group:id, upto});
    if (!out.ok) { seenGroups[id] = previous; return; }
    if (groupsById[id] && (groupsById[id].last?.id || 0) <= upto) { groupsById[id].unread = 0; renderGroups(); }
  } catch (e) { seenGroups[id] = previous; }
}

let mentionChoices = [], mentionIndex = 0, mentionStart = 0, mentionEnd = 0;
function hideMentions() { $("mentions").hidden = true; $("text").setAttribute("aria-expanded", "false"); $("text").removeAttribute("aria-activedescendant"); }
function updateMentions() {
  if (!selectedGroup || $("text").disabled) { hideMentions(); return; }
  const input = $("text"), before = input.value.slice(0, input.selectionStart);
  const match = before.match(/(?:^|[^\\p{L}\\p{N}_.@-])@([^\\s]*)$/u);
  if (!match) { hideMentions(); return; }
  mentionStart = before.length - match[1].length - 1; mentionEnd = input.selectionStart;
  const g = groupsById[selectedGroup], query = match[1].toLowerCase();
  mentionChoices = [...groupCandidates(g), {name:"user", kind:"human", online:true}]
    .filter(a => a.name.toLowerCase().startsWith(query));
  if (!mentionChoices.length) { hideMentions(); return; }
  mentionIndex = 0; renderMentions();
}
function renderMentions() {
  const list = $("mentions"); list.replaceChildren(); list.hidden = false;
  $("text").setAttribute("aria-expanded", "true");
  mentionChoices.forEach((a, i) => {
    const row = el("button", "mention-option" + (i === mentionIndex ? " active" : ""));
    row.id = "mention-" + i; row.setAttribute("role", "option"); row.setAttribute("aria-selected", String(i === mentionIndex));
    const info = el("div", "info"); info.append(el("div", "name", a.name), el("div", "tsub", a.title || (a.name === "user" ? "用户" : a.kind)));
    row.append(avatar(a.name, a.online), info);
    if (!groupsById[selectedGroup].members.includes(a.name)) row.append(el("span", "invite", "发送时拉进群"));
    row.onmousedown = e => e.preventDefault(); row.onclick = () => insertMention(a.name); list.append(row);
  });
  $("text").setAttribute("aria-activedescendant", "mention-" + mentionIndex);
  $("mention-" + mentionIndex)?.scrollIntoView({block:"nearest"});
}
function insertMention(name) {
  const t = $("text"); t.setRangeText("@" + name + " ", mentionStart, mentionEnd, "end");
  hideMentions(); t.focus(); updateSend(); autosize();
}

// ---- 团队：配对同事、开关团队连接、我的名字 ----
let codeTimer = null;
async function openTeam() {
  closeSide();
  const body = el("div", "team");
  openModal("团队", body, () => { clearInterval(codeTimer); codeTimer = null; });
  await renderTeam(body);
}

async function renderTeam(body) {
  const t = await api("/api/team");
  if (!t.me) return;
  clearInterval(codeTimer);
  body.textContent = "";

  const s1 = tsec("我的名字", "同事那边会看到你叫这个名字");
  const nameIn = el("input", "tin");
  nameIn.value = t.me.name; nameIn.maxLength = 32;
  const save = el("button", "btn", "保存");
  save.onclick = async () => flash(s1, await post("/api/team/me", { name: nameIn.value.trim() }));
  s1.append(trow(nameIn, save));

  const s2 = tsec("让别人加我", "开启后在 " + t.share.port + " 端口等同事连进来，只接受配对过的人");
  const sw = el("button", "switch" + (t.share.on ? " on" : ""));
  sw.setAttribute("aria-label", "团队连接");
  sw.onclick = async () => {
    const out = await post("/api/team/share", { on: !t.share.on });
    if (!out.ok) return flash(s2, out);
    await renderTeam(body);
  };
  s2.append(trow(el("span", "grow", t.share.on ? "团队连接已开启" : "团队连接已关闭"), sw));
  if (t.share.on) {
    // 第一个是默认出口网卡的地址，最可能是对的；其余多半是 VMware / WSL 之类的虚拟网卡
    const addrs = (t.share.addresses.length ? t.share.addresses : ["本机 IP"]).map(ip => ip + ":" + t.share.port);
    s2.append(el("div", "mlabel", "把下面的地址和配对码告诉同事："), el("div", "addr", addrs[0]));
    if (addrs.length > 1) s2.append(el("div", "mnote", "连不上时试试其他网卡：" + addrs.slice(1).join("、")));
    let code = t.code;
    if (!code) {
      const out = await post("/api/team/code", {});
      if (out.ok) code = { code: out.code, expires_in: out.expires_in };
    }
    if (code) {
      const left = el("span", "mnote");
      const again = el("button", "btn", "换一个");
      again.onclick = async () => { await post("/api/team/code", {}); renderTeam(body); };
      s2.append(trow(el("span", "code", code.code), left, again));
      const end = Date.now() + code.expires_in * 1000;
      const tick = () => {
        const s = Math.max(0, Math.round((end - Date.now()) / 1000));
        left.textContent = s ? "一次性，" + Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0") + " 后失效" : "已失效，点「换一个」";
      };
      tick();
      codeTimer = setInterval(tick, 1000);
    }
  }

  const s3 = tsec("我去加别人", "输入同事网页上显示的地址和配对码");
  const addrIn = el("input", "tin");
  addrIn.placeholder = "192.168.1.5:__PEER_PORT__";
  const codeIn = el("input", "tin code-in");
  codeIn.placeholder = "6 位配对码"; codeIn.maxLength = 6; codeIn.inputMode = "numeric";
  const join = el("button", "btn", "配对");
  join.onclick = async () => {
    join.disabled = true;
    const out = await post("/api/team/join", { address: addrIn.value, code: codeIn.value.trim() });
    join.disabled = false;
    if (out.ok) { agentJson = ""; poll(); await renderTeam(body); flash(body.querySelector(".tsec:nth-child(3)"), out); }
    else flash(s3, out);
  };
  s3.append(trow(addrIn, codeIn, join));

  const s4 = tsec("已配对 · " + t.peers.length);
  if (!t.peers.length) s4.append(el("div", "mnote", "还没有同事"));
  for (const p of t.peers) {
    const r = el("div", "prow");
    const info = el("div", "info");
    info.append(el("div", "name", p.alias),
      el("div", "cwd", (p.name !== p.alias ? p.name + " · " : "") + p.address + " · " + (p.online ? "在线" : "离线")));
    if (p.error) info.append(el("div", "perr", "联系不上：" + p.error));
    const rm = el("button", "btn danger", "解除配对");
    rm.onclick = async () => {
      if (!confirm("和 " + p.alias + " 解除配对？聊天记录会保留，之后可以重新配对。")) return;
      await post("/api/team/remove", { name: p.alias });
      agentJson = ""; poll();
      await renderTeam(body);
    };
    r.append(avatar(p.alias, p.online, true, "remote"), info, rm);
    s4.append(r);
  }

  const s5 = el("div", "tsec");
  s5.append(el("div", "mnote",
    "同事发来的消息只会到你这里，不会自动交给 agent；需要时点消息上的「转发」。" +
    "消息在网络上是明文：同一个局域网直接用，跨网络请用 Tailscale 这类自带加密的组网工具。" +
    "Windows 第一次开启团队连接时可能弹出防火墙提示，选「允许」；两台电脑的时间要准（相差超过 5 分钟会被拒绝）。"));
  body.append(s1, s2, s3, s4, s5);
}

// ---- 自动唤醒：总开关、来回上限、每个 agent 的开关 ----
let wakeInfo = null;
const lastWoken = {};

function renderWakeBtn() {
  const c = $("wakeCur"), w = wakeInfo;
  c.textContent = !w ? "" : !w.running ? "未运行" : w.paused ? "已暂停" : "开启";
  c.classList.toggle("warn", !!w && (!w.running || w.paused));
}

function wakeBadge(name) {  // 侧栏名字后面的小闪电：这个 agent 收到点名消息会被叫醒
  const w = wakeInfo && wakeInfo.agents[name];
  if (!w || w.reason || !w.enabled) return null;
  const b = icon("bolt", 13);
  b.classList.add("wk");
  if (!wakeInfo.running || wakeInfo.paused) {
    b.classList.add("idle");
    b.title = wakeInfo.paused ? "自动唤醒已暂停" : "自动唤醒没有在运行";
  } else if (w.limited) {
    b.classList.add("limit");
    b.title = w.note;
  } else {
    b.title = "收到点名消息会被自动唤醒" + (w.hint ? "\\n" + w.hint : "") + (w.note ? "\\n最近：" + w.note : "");
  }
  return b;
}

async function openWake() {
  closeSide();
  const body = el("div", "team");
  openModal("自动唤醒", body);
  renderWake(body, await api("/api/wake"));
}

function renderWake(body, w) {
  if (!w || !w.agents) return;
  wakeInfo = w;
  renderWakeBtn();
  body.textContent = "";

  const s1 = tsec("收到消息时叫醒 agent", "有人点名发消息给开着的 agent 时，叫它自己去读、去处理。发给所有人的广播不会叫醒。");
  const sw = el("button", "switch" + (w.paused ? "" : " on"));
  sw.setAttribute("aria-label", "自动唤醒");
  sw.onclick = async () => renderWake(body, await post("/api/wake/pause", { paused: !w.paused }));
  s1.append(trow(el("span", "grow", w.paused ? "已暂停" : "已开启"), sw));
  if (!w.running) s1.append(el("div", "merr", "现在没有在运行：kopitiam web 是不是用 --no-wake 启动的？也可以另开一个终端运行 kopitiam wake。"));

  const s2 = tsec("agent 之间的来回上限",
    "从你上次说话算起，两个 agent 之间连续来回超过这个次数，就不再互相叫醒（消息照常送到）。默认 " +
    w.default_rounds + "；填 0 = 只有你发的消息才会叫醒 agent。");
  const n = el("input", "tin rounds");
  n.type = "number"; n.min = 0; n.max = 100; n.value = w.max_rounds;
  const save = el("button", "btn", "保存");
  save.onclick = async () => {
    const out = await post("/api/wake/rounds", { n: n.value });
    if (!out.ok) return flash(s2, out);
    renderWake(body, out);
    flash(body.querySelector(".tsec:nth-child(2)"), out);
  };
  s2.append(trow(n, el("span", "mnote grow", "个来回"), save));

  const names = Object.keys(w.agents);
  const s3 = tsec("开着的 agent · " + names.length);
  if (!names.length) s3.append(el("div", "mnote", "现在没有开着的 agent（没开着的不会被叫醒）"));
  for (const name of names) {
    const a = w.agents[name];
    const r = el("div", "prow");
    const info = el("div", "info");
    const line = el("div", "line1");
    line.append(el("span", "name", name), el("span", "kind", kindLabel(a.kind)));
    info.append(line, el("div", "cwd" + (a.warn ? " warn" : ""), a.reason || a.note || a.hint || "可以叫醒"));
    info.title = [a.reason || a.note, a.hint].filter(Boolean).join("\\n");
    r.append(avatar(name, true, true, a.kind), info);
    if (a.supported) {  // 还没适配的 agent 不给开关，免得以为它会被叫醒
      const s = el("button", "switch" + (a.enabled ? " on" : ""));
      s.setAttribute("aria-label", name + " 的自动唤醒");
      s.onclick = async () => renderWake(body, await post("/api/wake/agent", { name, enabled: !a.enabled }));
      r.append(s);
    }
    s3.append(r);
  }

  const s4 = el("div", "tsec");
  s4.append(el("div", "mnote",
    "Claude Code、pi、dsh：各运行一次 kopitiam setup claude / pi / dsh，然后重开它们（Claude 不需要登录 claude.ai 账号）。" +
    "Codex 桌面版和命令行不用装就能叫醒；再装上 kopitiam setup codex，万一叫不醒还会弹 Windows 通知提醒你。" +
    "被叫醒的 agent 用的是它当前会话的权限：开了跳过权限的会话，别的 agent 也能让它执行命令。"));
  body.append(s1, s2, s3, s4);
}

// ---- 转发：把一条消息转给本机 agent 或同事 ----
function openForward(m) {
  const body = el("div", "fwd");
  const text = m.text || "";
  body.append(el("div", "mlabel", shown(m.sender) + " 的消息 #" + m.id + (m.files.length ? "（附件 " + m.files.length + " 个，会一起转发）" : "")),
              el("div", "fwd-preview", text.length > 200 ? text.slice(0, 200) + "…" : text || "（只有附件）"));
  const chosen = new Set();
  const go = el("button", "bigbtn", "转发");
  go.disabled = true;
  const list = el("div", "pick");
  const cands = Object.values(agentsByName)
    .filter(a => !a.hidden && a.name !== "user" && a.name !== m.sender)
    .sort((x, y) => (y.online - x.online) || ((x.kind === "remote") - (y.kind === "remote")));
  for (const a of cands) {
    const row = el("label", "pick-row");
    const cb = el("input");
    cb.type = "checkbox";
    cb.onchange = () => { cb.checked ? chosen.add(a.name) : chosen.delete(a.name); go.disabled = !chosen.size; };
    row.append(cb, avatar(a.name, a.online, true, a.kind), el("span", "name", a.name), el("span", "kind", kindLabel(a.kind)));
    list.append(row);
  }
  if (!cands.length) list.append(el("div", "mnote", "没有可以转发的对象"));
  go.onclick = async () => {
    go.disabled = true;
    const out = await post("/api/forward", { id: m.id, to: [...chosen] });
    if (!out.ok) { go.disabled = false; return flash(body, out); }
    closeModal(); poll();
  };
  body.append(list, el("div", "mnote", "转发的消息会标明「转发自 " + shown(m.sender) + "」。转给 agent 时，它会当作参考信息。"), go);
  openModal("转发", body);
}

// ---- 发往别的电脑的消息：已送达 / 等待送达 ----
const deliveryEls = {};
function setDelivery(id, d) {
  const box = deliveryEls[id];
  if (!box || !d) return;
  const sent = d.state === "sent";
  box.className = "dstate " + (sent ? "sent" : "pending");
  box.title = sent ? "" : (d.error ? "上次失败：" + d.error + "（会自动重试）" : "对方不在线时会自动重试");
  box.replaceChildren(icon(sent ? "check" : "clock", 12), el("span", "", sent ? "已送达" : "等待送达…"));
  // 失败原因直接写出来（以前只在悬停提示里，很容易看不到）
  if (!sent && d.error) box.append(el("span", "derr", "上次失败：" + d.error + "（会自动重试）"));
}

function logoMark(size) {
  const m = el("span", "mark");
  m.innerHTML = svg(LOGO, size, 30, 2.1);
  return m;
}

// ---- 说话：某个 agent 发来新消息时，它的机器人张嘴几秒 ----
const talking = {};
function startTalk(name) {
  talking[name] = Date.now() + 2600;
  refreshTalk();
  setTimeout(refreshTalk, 2700);
}
const woke = {};  // 刚被自动唤醒的 agent：头像跳一下、天线灯闪
function startWoke(name) {
  woke[name] = Date.now() + 2400;
  refreshTalk();
  setTimeout(refreshTalk, 2500);
}
function refreshTalk() {
  const now = Date.now();
  for (const row of document.querySelectorAll(".agent")) {
    const b = row.querySelector(".bot");
    if (b) b.classList.toggle("talk", (talking[row.dataset.name] || 0) > now);
    if (b) b.classList.toggle("woke", (woke[row.dataset.name] || 0) > now);
  }
  const cb = $("convAvatar").querySelector(".bot");
  if (cb) cb.classList.toggle("talk", (talking[selected] || 0) > now);
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
  const j = JSON.stringify(agents) + "|" + selected + "|" + JSON.stringify(wakeInfo);
  if (j === agentJson) return;
  agentJson = j;
  const shownAgents = agents.filter(a => !a.hidden);  // 归档的、离线超过 7 天的收进「已归档 / 隐藏」子页面
  const hiddenN = agents.length - shownAgents.length;
  const team = shownAgents.filter(a => a.kind === "remote").sort((x, y) => y.online - x.online);
  const local = shownAgents.filter(a => a.kind !== "remote");
  const online = local.filter(a => a.online), offline = local.filter(a => !a.online);
  const n = online.filter(a => a.name !== "user").length;
  $("brandsub").textContent = n ? n + " 个 agent 在线" : "本地聊天室 · 暂无 agent 在线";
  const box = $("agents");
  box.textContent = "";
  box.append(el("div", "section", "在线 · " + online.length));
  for (const a of online) box.append(agentRow(a));
  // 团队：别的电脑上的同事（人和人之间；他们的消息只到你这里）
  const th = el("div", "section team-head");
  const addBtn = el("button", "mini");
  addBtn.append(icon("plus", 13));
  addBtn.title = "添加同事 / 团队设置";
  addBtn.onclick = openTeam;
  th.append(el("span", "grow", "团队 · " + team.filter(a => a.online).length + "/" + team.length), addBtn);
  box.append(th);
  for (const a of team) box.append(agentRow(a));
  if (!team.length) {
    const hint = el("button", "team-hint");
    hint.append(icon("plus", 14), el("span", "", "添加同事，跨电脑互发消息"));
    hint.onclick = openTeam;
    box.append(hint);
  }
  if (offline.length) {
    // 离线的默认折叠；轮询重绘时保持用户上次的展开状态
    const det = el("details", "offline");
    det.open = offlineOpen;
    det.ontoggle = () => { offlineOpen = det.open; };
    const sum = el("summary", "section");
    sum.append(icon("chev", 12), document.createTextNode("离线 · " + offline.length));
    det.append(sum);
    for (const a of offline) det.append(agentRow(a));
    box.append(det);
  }
  if (hiddenN) {
    const link = el("button", "hidden-link");
    link.append(icon("archive", 14), el("span", "grow", "已归档 / 隐藏 · " + hiddenN), icon("chev", 12));
    link.onclick = openHidden;
    box.append(link);
  }
  if (selected) updateConvbar();
}

// ---- 「已归档 / 隐藏」子页面 ----
function setArchived(name, value) { return agentOp("/api/archive", { name, archived: value }); }

async function openHidden() {
  closeSide();
  $("main").classList.add("page");
  $("hiddenPage").hidden = false;
  await loadHidden();
}

function closeHidden() {
  $("main").classList.remove("page");
  $("hiddenPage").hidden = true;
}

async function loadHidden() {
  const r = await api("/api/hidden");
  const list = $("hiddenList");
  list.textContent = "";
  const agents = r.agents || [];
  if (!agents.length) { list.append(el("div", "hempty", "没有归档或隐藏的 agent")); return; }
  const kept = agents.filter(a => !a.trashed_at), trash = agents.filter(a => a.trashed_at);
  for (const a of kept) list.append(hiddenCard(a));
  if (trash.length) {
    const head = el("div", "hsec");
    head.append(el("span", "", "回收站 · " + trash.length),
      el("span", "note", "到期连同它发的、只发给它的消息一起删除；这期间消息照常显示，随时可以恢复"));
    list.append(head);
    for (const a of trash) list.append(hiddenCard(a));
  }
}

// 归档 / 回收站 / 删除共用：发请求，失败弹提示，成功刷新子页面和成员列表
async function agentOp(path, body, ask) {
  if (ask && !confirm(ask)) return;
  const out = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!out.ok) { alert(out.message || "操作失败"); return; }
  agentJson = "";
  if (!$("hiddenPage").hidden) await loadHidden();
  if (path === "/api/purge") await reloadStream();  // 它的消息也删了，页面上已经显示的要拿掉
  else poll();
}

// 消息流只会追加新消息；有消息被删掉时清空重新加载
async function reloadStream() {
  $("list").textContent = "";
  known.clear();
  lastId = 0;
  booted = false;  // 重新加载的不播入场动画
  await poll();
  if (selected) await loadHistory(selected);
  if (selectedGroup) await loadGroupHistory(selectedGroup, true);
}

function opBtn(iconName, label, cls, onclick, title) {
  const b = el("button", "btn" + (cls ? " " + cls : ""));
  b.append(icon(iconName, 14), el("span", "", label));
  if (title) b.title = title;
  b.onclick = onclick;
  return b;
}

function hiddenCard(a) {
  const card = el("div", "hcard" + (a.trashed_at ? " trash" : ""));
  const info = el("div", "info");
  const line1 = el("div", "line1");
  line1.append(el("span", "name", a.name), el("span", "kind", a.kind));
  if (a.trashed_at) line1.append(el("span", "kind", "回收站"));
  else if (a.archived_at) line1.append(el("span", "kind", "已归档"));
  const msgs = a.messages ? a.messages + " 条相关消息" : "没有消息";
  const when = a.trashed_at ? "将于 " + fmtTime(a.purge_at) + " 删除"
    : (a.archived_at ? "归档于 " + fmtTime(a.archived_at) + " · " : "") +
      (a.online ? "在线" : "最后出现 " + fmtTime(a.last_seen) + " · " + a.ago);
  info.append(line1, el("div", "cwd", a.title ? a.title + " · " + a.cwd : a.cwd),
    el("div", "hmeta", when + " · " + msgs));
  const acts = el("div", "acts");
  acts.append(opBtn("chat", "查看对话", "", () => select(a.name)));
  if (a.trashed_at) {
    acts.append(opBtn("restore", "恢复", "", () => agentOp("/api/trash", { name: a.name, trashed: false }),
      "拿出回收站，放回原来的地方"));
    acts.append(opBtn("trash", "永久删除", "danger", () => agentOp("/api/purge", { name: a.name },
      "现在就永久删除 " + a.name + "？它发的、只发给它的消息会一起删除（" + msgs + "），不能恢复。")));
  } else {
    if (a.archived_at) {
      acts.append(opBtn("restore", "恢复", "", () => setArchived(a.name, false), "放回成员列表，恢复自动唤醒"));
    }
    if (a.messages || a.online) {
      // 有消息的直接删会丢掉历史里的发送者，在线的还在用这个身份：先进回收站，到期再删
      acts.append(opBtn("trash", "移到回收站", "danger", () => agentOp("/api/trash", { name: a.name, trashed: true },
        "把 " + a.name + " 移到回收站？30 天后连同它发的、只发给它的消息一起删除（" + msgs + "）。" +
        "这期间随时可以恢复，它自己再用 kopitiam 也会自动恢复。")));
    } else {
      acts.append(opBtn("trash", "删除", "danger", () => agentOp("/api/forget", { name: a.name },
        "删除 " + a.name + "？它没有任何消息，删除后不能恢复。")));
    }
  }
  card.append(avatar(a.name, false, true), info, acts);
  return card;
}

function agentRow(a) {
  const div = el("div", "agent" + (a.online ? "" : " off") + (a.name === selected ? " active" : ""));
  div.title = a.title ? a.title + "\\n" + a.cwd : a.cwd;
  div.dataset.name = a.name;
  const info = el("div", "info");
  const line1 = el("div", "line1");
  line1.append(el("span", "name", a.name));
  if (a.name !== "user") line1.append(el("span", "kind", kindLabel(a.kind)));
  // 同事的地址端口是 0：配对时对方没开团队连接，发给他的消息送不到
  const noShare = a.kind === "remote" && a.cwd.endsWith(":0");
  const sub = a.name === "user" ? "你 · 网页"
    : a.kind === "remote" ? ((a.peer_name && a.peer_name !== a.name ? a.peer_name + " · " : "")
        + (noShare ? "没开团队连接，收不到消息 · " + a.cwd.slice(0, -2) : (a.online ? "在线" : "离线") + " · " + a.cwd))
    : a.title ? a.title + " · " + a.cwd : a.cwd;
  info.append(line1, el("div", "cwd" + (noShare ? " warn" : ""), sub));
  const av = avatar(a.name, a.online, false, a.kind);
  if ((talking[a.name] || 0) > Date.now()) av.classList.add("talk");
  if ((woke[a.name] || 0) > Date.now()) av.classList.add("woke");
  const wb = wakeBadge(a.name);
  if (wb) av.append(wb);
  div.append(av, info);
  if (a.name !== "user") {
    const copy = el("button", "act");
    copy.append(icon("copy", 14));
    copy.title = "复制名字";
    copy.onclick = (e) => {
      e.stopPropagation();  // 只复制，不切换对话
      navigator.clipboard.writeText(a.name).then(() => {
        copy.replaceChildren(icon("check", 14));
        setTimeout(() => copy.replaceChildren(icon("copy", 14)), 1200);
      });
    };
    const add = el("button", "act add");
    add.append(icon("plus", 15));
    add.title = "加到「发给」（可以同时发给多个）";
    add.onclick = (e) => {
      e.stopPropagation();  // 只加收件人，不切换对话
      if (selectedGroup) {
        if (a.kind === "remote") { select(a.name); return; }
        const input = $("text");
        if (input.disabled) return;
        input.setRangeText("@" + a.name + " ", input.selectionStart, input.selectionEnd, "end");
        input.focus(); updateSend(); autosize(); return;
      }
      const cur = $("to").value.split(",").map(s => s.trim()).filter(Boolean);
      if (!cur.includes(a.name)) cur.push(a.name);
      $("to").value = cur.join(", ");
    };
    const ops = el("div", "ops");
    ops.append(copy, add);
    if (a.kind !== "remote") {
      const arch = el("button", "act arch");
      arch.append(icon("archive", 14));
      arch.title = "归档：收进「已归档 / 隐藏」，不再叫醒它（它自己再用 kopitiam 会自动回来）";
      arch.onclick = (e) => { e.stopPropagation(); setArchived(a.name, true); };
      ops.append(arch);
    }
    div.append(ops);
  }
  // 点 agent / 同事 = 和它对话；点 user = 我的消息（在那里发送 = 发给自己）；再点一次回到全部消息
  div.onclick = () => select(a.name === selected ? null : a.name);
  return div;
}

function involves(m, name) {
  // 和它有关：它发的、发给它的、以及广播（它也收得到）
  return m.sender === name || m.to.includes(name) || m.to.length === 0;
}

let emptyKey = "logo";
function updateEmpty() {
  const any = [...$(selectedGroup ? "groupList" : "list").children].some(e => !e.hidden);
  $("empty").hidden = any;
  $("emptyTitle").textContent = selectedGroup ? groupsById[selectedGroup]?.title || "群聊" : selected === "user" ? "我的消息" : selected || "kopitiam";
  $("emptySub").textContent = selectedGroup ? "发第一条消息，或用 @名字 把伙伴拉进来" : selected === "user" ? "在这里发送 = 发给自己，当备忘用"
    : selected ? "和 " + selected + " 还没有消息" : "还没有消息，在下面写第一句吧";
  // 选中某个 agent 时换成它的大号头像；只在变化时重建，否则每次轮询都会把动画打断
  const a = selected && selected !== "user" ? agentsByName[selected] : null;
  const key = selected ? selected + "|" + !!(a && a.online) : "logo";
  if (key !== emptyKey) {
    emptyKey = key;
    $("emptyArt").replaceChildren(selected ? avatar(selected, !!(a && a.online)) : logoMark(34));
  }
}

function applyFilter() {
  for (const e of $("list").children) e.hidden = selected !== null && !involves(e._m, selected);
  updateEmpty();
}

function updateConvbar() {
  if (selectedGroup) { renderGroupHeader(); return; }
  const a = selected && agentsByName[selected];
  $("convtitle").textContent = selected === "user" ? "我的消息" : selected ? "与 " + selected + " 的对话" : "全部消息";
  $("convsub").textContent = selected === "user" ? "发给你的、你发的都在这里；在这里发送 = 发给自己（备忘）"
    : selected ? (a ? kindLabel(a.kind) + " · " + (a.online ? "在线" : "离线") + " · " + (a.title ? a.title + " · " : "") + a.cwd : "")
    : "所有人之间的消息都在这里";
  $("showAll").hidden = !selected;
  $("convAvatar").hidden = !selected;
  $("convAvatar").replaceChildren(...(selected ? [avatar(selected, !!(a && a.online))] : []));
  refreshTalk();
}

function closeSide() { $("side").classList.remove("open"); $("backdrop").classList.remove("show"); }

function select(name) {
  closeHidden();
  hideMentions(); selectedGroup = null; groupHistorySeq++; groupHistoryKey = "";
  document.body.classList.remove("in-group");
  $("groupHeader").hidden = true; $("groupOptions").hidden = true;
  $("list").hidden = false; $("groupList").hidden = true;
  $("to").closest(".torow").hidden = false;
  $("text").disabled = restartRequired; $("pick").disabled = restartRequired; $("text").placeholder = "写点什么…";
  selected = name;
  updateSend(); renderGroups();
  updateConvbar();
  $("to").value = name || "";
  agentJson = "";  // 强制重绘左栏，更新高亮
  poll();
  applyFilter();
  closeSide();
  const s = $("stream"); s.scrollTop = s.scrollHeight;
  $("text").focus();
  if (name) loadHistory(name);
}

// 页面一开始只加载最新的 200 条；进入某个对话时，再从服务器补全它最近 200 条历史
async function loadHistory(name) {
  const r = await api("/api/messages?with=" + encodeURIComponent(name));
  if (selected !== name) return;  // 等待期间已经切到别的对话
  for (const m of (r.messages || [])) addMessage(m, true);
  applyFilter();
  const s = $("stream"); s.scrollTop = s.scrollHeight;
}

function messageNode(m) {
  const mine = m.sender === "user";
  let div;
  if (/^（.+ 改名为 .+）$/.test(m.text) && !m.files.length) {
    div = el("div", "notice");  // 改名通知：居中灰色小字
    div.append(el("span", "", m.text.slice(1, -1)));
  } else {
    div = el("div", "msg" + (mine ? " mine" : ""));
    if (!mine) div.append(avatar(m.sender, true, true, m.sender_kind));  // 消息里的头像不加动画，免得满屏都在动
    const body = el("div", "body");
    const meta = el("div", "meta");
    const who = el("span", "who", shown(m.sender));
    who.style.setProperty("--h", hue(m.sender));
    if (!mine) {  // 点发送者的名字 = 复制，和别的 agent 说起它时直接粘贴
      who.classList.add("copyable");
      who.title = "点击复制名字";
      who.onclick = () => navigator.clipboard.writeText(m.sender).then(() => {
        who.textContent = "已复制";
        setTimeout(() => { who.textContent = shown(m.sender); }, 1200);
      });
    }
    const fwd = el("button", "fwdbtn");
    fwd.append(icon("fwd", 14));
    fwd.title = "转发给 agent 或同事";
    fwd.onclick = () => openForward(m);
    const recipients = m.group ? (m.to_all ? "全体" : m.to.map(n => "@" + n).join("、")) : (m.to.length ? m.to.map(shown).join("、") : "所有人");
    meta.append(who, el("span", "", "→ " + recipients),
                el("span", "", fmtTime(m.time)), el("span", "id", "#" + m.id), fwd);
    if (m.group) {
      const label = el("button", "group-label", "群聊·" + (groupsById[m.group]?.title || m.group));
      label.dataset.group = m.group; label.onclick = () => selectGroup(m.group); meta.append(label);
    }
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
        card.append(icon("file", 18), fn, fileActions(f));
        files.append(card);
      }
      body.append(files);
    }
    if (m.delivery) {  // 发往别的电脑的消息
      deliveryEls[m.id] = el("div", "dstate");
      body.append(deliveryEls[m.id]);
      setDelivery(m.id, m.delivery);
    }
    div.append(body);
  }
  div._m = m;
  return div;
}

function addMessage(m, backfill) {
  // 即使历史补全已显示这条消息，也要推进本轮服务器游标。
  if (!backfill && m.id > lastId) lastId = m.id;
  if (known.has(m.id)) return;
  known.add(m.id);
  const div = messageNode(m);
  div.hidden = selected !== null && !involves(m, selected);
  const fresh = booted && !backfill;
  if (fresh) div.classList.add("enter");  // 首次加载和补全的历史不播动画，之后的新消息淡入
  // 按 id 顺序插入（补全的历史比现有的旧，要插到前面）
  const list = $("list"), last = list.lastElementChild;
  if (last && last._m.id > m.id) list.insertBefore(div, [...list.children].find(e => e._m.id > m.id));
  else list.append(div);
  if (fresh && m.sender !== "user" && div.className.startsWith("msg")) startTalk(m.sender);
}

// 连不上网页服务时在顶上提示；否则页面看起来一切正常，其实已经收不到新消息
function setConn(state) {
  const b = $("conn");
  if ((b.dataset.state || "") === state) return;
  b.dataset.state = state;
  b.hidden = !state;
  if (state) b.replaceChildren(el("span", "dot"), el("span", "", state === "token"
    ? "这个网页已失效：kopitiam web 重启过，请打开终端里新打印的地址"
    : "连不上 kopitiam web（可能已经关掉了），新消息不会显示；重新运行 kopitiam web 后用新地址打开"));
}

async function poll() {
  let st;
  try { st = await api("/api/state?after=" + lastId); } catch (e) { setConn("down"); return; }
  if (st.error === "forbidden") { setConn("token"); return; }  // token 不对：服务重启过
  setConn("");
  applyRestoreState(st.restart_required);
  if (restartRequired) return;
  const s = $("stream");
  const atBottom = s.scrollTop + s.clientHeight >= s.scrollHeight - 40;
  groupsById = Object.fromEntries((st.groups || []).map(g => [g.id, g]));
  for (const m of (st.messages || [])) addMessage(m);
  for (const [id, d] of Object.entries(st.deliveries || {})) setDelivery(id, d);
  wakeInfo = st.wake || null;
  renderWakeBtn();
  renderAgents(st.agents || []);
  renderGroups();
  if (selectedGroup) { renderGroupHeader(); loadGroupHistory(selectedGroup); }
  for (const [name, a] of Object.entries((wakeInfo || {}).agents || {})) {
    if (name in lastWoken && a.woken_at && a.woken_at !== lastWoken[name]) startWoke(name);
    lastWoken[name] = a.woken_at;
  }
  updateEmpty();
  if (!selectedGroup && (st.messages || []).length && atBottom) s.scrollTop = s.scrollHeight;
  booted = true;
}

// ---- 附件：打开（默认）/ 打开文件夹 / 复制路径 ----
let fileMenu = null;  // 当前弹出的附件菜单（同一时间只开一个）

function closeFileMenu() { if (fileMenu) { fileMenu.remove(); fileMenu = null; } }

// 按钮上短暂显示结果（「已打开」「已复制」），然后恢复原样。
// 原样只记第一次的：连着点两下时，第二次记到的是第一次的提示，恢复成它就卡住了
function flash(btn, iconName, text) {
  if (!btn._orig) btn._orig = [...btn.childNodes];
  clearTimeout(btn._flashT);
  btn.replaceChildren(icon(iconName, 14), el("span", "", text));
  btn._flashT = setTimeout(() => btn.replaceChildren(...btn._orig), 1200);
}

async function openFile(f, folder, btn) {
  const name = decodeURIComponent(f.url.split("?")[0].split("/").pop());  // 附件目录里的文件名
  const out = await api("/api/open", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, folder }) });
  if (!out.ok) { alert(out.message || "打不开"); return; }
  if (out.revealed) alert(out.message);  // 可执行文件：只在文件夹里选中了它
  else if (btn) flash(btn, "check", out.message);
}

function fileActions(f) {
  const box = el("div", "split");
  const main = el("button", "btn main");
  main.append(icon("open", 14), el("span", "", "打开"));
  main.title = "用默认程序打开（" + f.path + "）";
  main.onclick = () => openFile(f, false, main);
  const more = el("button", "btn more");
  more.append(icon("down", 14));
  more.title = "更多";
  more.setAttribute("aria-label", "更多");
  more.onclick = (e) => {
    e.stopPropagation();
    const wasOpen = fileMenu && fileMenu.parentNode === box;
    closeFileMenu();
    if (wasOpen) return;
    const menu = el("div", "fmenu");
    const folder = el("button");
    folder.append(icon("folder", 14), el("span", "", "打开文件夹"));
    folder.onclick = () => { closeFileMenu(); openFile(f, true, main); };
    const copy = el("button");
    copy.append(icon("copy", 14), el("span", "", "复制路径"));
    copy.onclick = () => navigator.clipboard.writeText(f.path).then(() => { closeFileMenu(); flash(main, "check", "已复制路径"); });
    menu.append(folder, copy);
    // 靠近消息区底部时向上弹，免得被输入框挡住
    const r = box.getBoundingClientRect(), s = $("stream").getBoundingClientRect();
    if (s.bottom - r.bottom < 110) menu.classList.add("up");
    box.append(menu);
    fileMenu = menu;
  };
  box.append(main, more);
  return box;
}

function renderChips() {
  const row = $("chiprow");
  row.textContent = "";
  pendingFiles.forEach((f, i) => {
    const chip = el("span", "chip", f.name);
    chip.title = f.path;
    const x = el("button");
    x.append(icon("x", 12));
    x.title = "移除";
    x.onclick = () => { pendingFiles.splice(i, 1); renderChips(); };
    chip.append(x);
    row.append(chip);
  });
  updateSend();
}

function updateSend() { $("send").disabled = restartRequired || !!groupsById[selectedGroup]?.archived_at || (!$("text").value.trim() && !pendingFiles.length); }

function autosize() {  // 输入框随内容变高，最高到 40% 屏幕高度
  const t = $("text");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight + 2, window.innerHeight * 0.4) + "px";
}

async function send() {
  if (restartRequired) return;
  const text = $("text").value;
  const to = $("to").value.split(",").map(s => s.trim()).filter(Boolean);
  if (groupsById[selectedGroup]?.archived_at) return;
  if (!text.trim() && !pendingFiles.length) return;
  let out;
  try {
    out = await api("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({text, ...(selectedGroup ? {group:selectedGroup} : {to}), files:pendingFiles.map(f => f.path)}),
    });
  } catch (e) { alert("发送失败：连不上 kopitiam web"); return; }
  if (!out.ok) { alert(out.message || "发送失败"); return; }
  $("text").value = "";
  hideMentions();
  autosize();
  pendingFiles = []; renderChips();
  poll();
}

async function upload(files) {
  if (restartRequired) return;
  for (const f of files) {
    const r = await api("/api/upload?name=" + encodeURIComponent(f.name), {
      method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: f,
    });
    if (r.ok) pendingFiles.push({ name: f.name, path: r.path });
  }
  renderChips();
}

// ---- 主题：选择只存在这个浏览器里（localStorage 不可用时就不记，照样能切换） ----
const THEMES = [
  { id: "auto",     name: "跟随系统", a: "#f3f4f6", b: "#000000" },
  { id: "obsidian", name: "曜石",     a: "#000000", b: "#fafafa" },
  { id: "light",    name: "浅色",     a: "#f3f4f6", b: "#3370ff" },
  { id: "dark",     name: "深色",     a: "#0f1115", b: "#5b8dff" },
  { id: "latte",    name: "拿铁",     a: "#f5ede3", b: "#a8612a" },
  { id: "kopi",     name: "咖啡",     a: "#17110e", b: "#d99a55" },
  { id: "ocean",    name: "深海",     a: "#0b1220", b: "#3cc8bd" },
  { id: "sakura",   name: "樱花",     a: "#fbf1f4", b: "#d9466f" },
];

function currentTheme() { return document.documentElement.dataset.theme || "auto"; }

function setTheme(id) {
  if (id === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = id;
  try { localStorage.setItem("kopitiam-theme", id); } catch (e) {}
  renderThemes();
}

function renderThemes() {
  const cur = THEMES.find(t => t.id === currentTheme()) || THEMES[0];
  $("themeCur").textContent = cur.name;
  const pop = $("themePop");
  pop.textContent = "";
  for (const t of THEMES) {
    const b = el("button", "theme-opt" + (t.id === cur.id ? " on" : ""));
    const sw = el("span", "swatch");
    sw.style.background = "linear-gradient(135deg, " + t.a + " 50%, " + t.b + " 50%)";
    b.append(sw, el("span", "", t.name));
    b.onclick = (e) => { e.stopPropagation(); setTheme(t.id); };  // 弹窗保持打开，方便对比
    pop.append(b);
  }
}

// 静态位置的图标和 logo
for (const s of document.querySelectorAll("[data-icon]")) s.replaceWith(icon(s.dataset.icon));
for (const m of document.querySelectorAll("[data-logo]")) m.innerHTML = svg(LOGO, Number(m.dataset.size || 20), 30, 2.1);

renderThemes();
$("themeBtn").onclick = (e) => { e.stopPropagation(); $("themePop").hidden = !$("themePop").hidden; };
document.addEventListener("click", (e) => {
  if (!$("themePop").contains(e.target)) $("themePop").hidden = true;
  if (fileMenu && !fileMenu.contains(e.target)) closeFileMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (fileMenu) closeFileMenu();
  else if (!$("modal").hidden) closeModal();
  else if (!$("themePop").hidden) $("themePop").hidden = true;
  else if (!$("hiddenPage").hidden) closeHidden();
});
$("mclose").onclick = closeModal;
$("wakeBtn").onclick = openWake;
$("backupBtn").onclick = openBackup;
$("modal").onclick = (e) => { if (e.target === $("modal")) closeModal(); };  // 点弹窗外面关闭
$("hiddenBack").onclick = closeHidden;
$("send").onclick = send;
$("groupOptions").onclick = () => openGroupEditor(groupsById[selectedGroup]);
$("showAll").onclick = () => select(null);
$("text").addEventListener("input", () => { updateSend(); autosize(); updateMentions(); });
$("text").addEventListener("click", updateMentions);
$("text").addEventListener("keydown", (e) => {
  if (e.isComposing) return;
  if (!$("mentions").hidden && !e.ctrlKey) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault(); mentionIndex = (mentionIndex + (e.key === "ArrowDown" ? 1 : -1) + mentionChoices.length) % mentionChoices.length;
      renderMentions(); return;
    }
    if (e.key === "Enter") { e.preventDefault(); insertMention(mentionChoices[mentionIndex].name); return; }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); hideMentions(); return; }
  }
  if (e.key === "Enter" && e.ctrlKey) { e.preventDefault(); send(); }
});
document.addEventListener("click", e => { if (e.target !== $("text") && !$("mentions").contains(e.target)) hideMentions(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden && selectedGroup) loadGroupHistory(selectedGroup); });
$("pick").onclick = () => $("file").click();
$("file").onchange = (e) => { upload([...e.target.files]); e.target.value = ""; };
$("menuBtn").onclick = () => { $("side").classList.add("open"); $("backdrop").classList.add("show"); };
$("backdrop").onclick = closeSide;
poll();
pollBackup();
setInterval(pollBackup, 1500);
setInterval(poll, 1500);
</script>
</body>
</html>
""".replace("__PEER_PORT__", str(PEER_PORT))
