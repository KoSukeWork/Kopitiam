"""只在临时目录运行的 WebDAV 测试服务器，不联系真实云盘。"""
import base64
import threading
import xml.etree.ElementTree as ET
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit


class FakeDAV(ThreadingHTTPServer):
    def __init__(self, root):
        self.root = root
        root.mkdir()
        (root / "dav").mkdir()
        self.calls = []
        self.fail = {}
        self.redirect = None
        self.expected_auth = "Basic " + base64.b64encode(b"tester:application-password").decode()
        super().__init__(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}/dav/"

    def close(self):
        self.shutdown()
        self.server_close()
        self.thread.join()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body=b"", headers=None):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def handle_method(self):
        self.server.calls.append((self.command, self.path, dict(self.headers)))
        if self.headers.get("Authorization") != self.server.expected_auth:
            return self.reply(401)
        if self.server.redirect:
            return self.reply(302, headers={"Location": self.server.redirect})
        if self.command in self.server.fail:
            return self.reply(self.server.fail[self.command])
        path = (self.server.root / unquote(urlsplit(self.path).path).lstrip("/")).resolve()
        if not path.is_relative_to(self.server.root.resolve()):
            return self.reply(403)
        if self.command == "MKCOL":
            if path.exists():
                return self.reply(405)
            path.mkdir()
            return self.reply(201)
        if self.command == "PUT":
            path.write_bytes(self.rfile.read(int(self.headers["Content-Length"])))
            return self.reply(201)
        if self.command == "MOVE":
            dest = self.server.root / unquote(urlsplit(self.headers["Destination"]).path).lstrip("/")
            if dest.exists():
                return self.reply(412)
            path.rename(dest)
            return self.reply(201)
        if not path.exists():
            return self.reply(404)
        if self.command == "GET":
            return self.reply(200, path.read_bytes())
        if self.command == "DELETE":
            path.unlink()
            return self.reply(204)
        if self.command == "PROPFIND":
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            root = ET.Element("{DAV:}multistatus")
            for f in [path, *path.iterdir()]:
                response = ET.SubElement(root, "{DAV:}response")
                ET.SubElement(response, "{DAV:}href").text = "/" + quote(f.relative_to(self.server.root).as_posix())
                stat = ET.SubElement(response, "{DAV:}propstat")
                ET.SubElement(stat, "{DAV:}status").text = "HTTP/1.1 200 OK"
                prop = ET.SubElement(stat, "{DAV:}prop")
                ET.SubElement(prop, "{DAV:}getcontentlength").text = str(f.stat().st_size)
                ET.SubElement(prop, "{DAV:}getlastmodified").text = formatdate(usegmt=True)
                rt = ET.SubElement(prop, "{DAV:}resourcetype")
                if f.is_dir():
                    ET.SubElement(rt, "{DAV:}collection")
            return self.reply(207, ET.tostring(root, encoding="utf-8"))
        self.reply(405)

    do_GET = do_PUT = do_MOVE = do_DELETE = do_MKCOL = do_PROPFIND = handle_method
