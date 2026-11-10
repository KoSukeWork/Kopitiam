"""标准库 WebDAV：只访问配置的 origin，凭据不跟随跨 origin 重定向。"""
from __future__ import annotations

import base64
import re
import socket
import ssl
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

NAME_RE = re.compile(r"kopitiam-[\w.\-]+\.kpbak\Z")
TIME_RE = re.compile(r"kopitiam-(?P<host>[\w.\-]+)-(?P<time>\d{8}-\d{6})\.kpbak\Z")
BLOCK = 1024 * 1024


class WebDAVError(ValueError):
    pass


def validate_url(url: str) -> str:
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        raise WebDAVError("WebDAV 地址不合法")
    url = url.strip()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise WebDAVError("WebDAV 地址不合法") from None
    if parts.username is not None or parts.password is not None:
        raise WebDAVError("WebDAV 地址不能包含用户名或密码")
    if not parts.hostname or parts.query or parts.fragment or "\\" in url:
        raise WebDAVError("WebDAV 地址不合法")
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")):
        raise WebDAVError("WebDAV 地址必须使用 HTTPS（只有本机测试地址允许 HTTP）")
    if port is not None and not 1 <= port <= 65535:
        raise WebDAVError("WebDAV 端口不合法")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/", "", ""))


def validate_folder(folder: str) -> str:
    if not isinstance(folder, str):
        raise WebDAVError("远程文件夹格式不对")
    folder = folder.strip("/") or "kopitiam"
    if ".." in folder or not re.fullmatch(r"[\w.\-/]+", folder) or any(p in ("", ".") for p in folder.split("/")):
        raise WebDAVError("远程文件夹只能包含字母、数字、下划线、点、横线和 /，不能有 ..")
    return folder


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise WebDAVError("备份文件名不合法")
    return name


def _origin(url: str):
    p = urlsplit(url)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(req.full_url) != _origin(newurl):
            raise WebDAVError(f"拒绝跨主机、跨端口或降级重定向（HTTP {code}），请检查 WebDAV 地址")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_error(code: int) -> WebDAVError:
    messages = {401: "用户名或应用密码不对（坚果云要用『应用密码』，不是登录密码）",
                403: "服务器拒绝访问，请检查文件夹权限", 404: "找不到文件或 WebDAV 路径，请检查地址",
                405: "服务器不支持这项 WebDAV 操作，请检查 WebDAV 地址", 507: "远程存储空间不够",
                412: "同名备份已存在，未覆盖远程文件"}
    return WebDAVError(f"{messages.get(code, 'WebDAV 请求失败')}（HTTP {code}）")


class _Upload:
    def __init__(self, file, total, progress):
        self.file, self.total, self.progress, self.sent = file, total, progress, 0

    def read(self, size=-1):
        data = self.file.read(min(size, BLOCK) if size >= 0 else BLOCK)
        self.sent += len(data)
        if self.progress:
            self.progress(self.sent, self.total)
        return data


class WebDAV:
    def __init__(self, url: str, user: str, password: str, folder: str = "kopitiam"):
        self.base = validate_url(url)
        self.folder = validate_folder(folder)
        self.root = self.base + quote(self.folder, safe="/") + "/"
        if not isinstance(user, str) or ":" in user or any(c in user for c in "\r\n"):
            raise WebDAVError("WebDAV 用户名不合法")
        self.auth = "Basic " + base64.b64encode((user + ":" + password).encode("utf-8")).decode("ascii")
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()), _Redirect())

    def _request(self, method, url, *, data=None, headers=None, accepted=()):
        req = urllib.request.Request(url, data=data, headers={"Authorization": self.auth, **(headers or {})}, method=method)
        try:
            return self.opener.open(req, timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code in accepted:
                exc.close()
                return None
            code = exc.code
            exc.close()
            raise _http_error(code) from None
        except (urllib.error.URLError, OSError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise WebDAVError("WebDAV 连接或传输超时，请稍后重试") from None
            if isinstance(reason, ssl.SSLError):
                raise WebDAVError("WebDAV 证书校验失败，请检查服务器证书和本机时间") from None
            raise WebDAVError("无法连接 WebDAV，请检查地址和网络") from None

    def ensure_dir(self) -> None:
        path = self.base
        for component in self.folder.split("/"):
            path += quote(component, safe="") + "/"
            response = self._request("MKCOL", path, accepted=(405,))
            if response is not None:
                response.close()

    def list(self) -> list[dict]:
        body = b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:getcontentlength/><d:getlastmodified/><d:resourcetype/></d:prop></d:propfind>'
        with self._request("PROPFIND", self.root, data=body, headers={"Depth": "1", "Content-Type": "application/xml"}) as response:
            # NOTE: 限制列表响应为 8 MiB；异常大或实体扩展 XML 不交给解析器。
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise WebDAVError("WebDAV 文件列表过大或格式不安全")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise WebDAVError("服务器没有返回有效的 WebDAV 文件列表") from None
        result = {}
        for node in root.findall("{DAV:}response"):
            href = node.findtext("{DAV:}href", "")
            url = urljoin(self.root, href)
            if _origin(url) != _origin(self.root):
                continue
            path, parent = unquote(urlsplit(url).path), unquote(urlsplit(self.root).path)
            if not path.startswith(parent):
                continue
            name = path[len(parent):]
            if not NAME_RE.fullmatch(name):
                continue
            props = None
            for propstat in node.findall("{DAV:}propstat"):
                if " 200 " in propstat.findtext("{DAV:}status", ""):
                    props = propstat.find("{DAV:}prop")
                    break
            if props is None or props.find("{DAV:}resourcetype/{DAV:}collection") is not None:
                continue
            match = TIME_RE.fullmatch(name)
            stamp = ""
            if match:
                try:
                    stamp = datetime.strptime(match["time"], "%Y%m%d-%H%M%S").isoformat()
                except ValueError:
                    pass
            try:
                size = max(0, int(props.findtext("{DAV:}getcontentlength", "0")))
            except ValueError:
                size = 0
            result[name] = {"name": name, "host": match["host"] if match else "未知电脑", "time": stamp,
                            "size": size, "modified": props.findtext("{DAV:}getlastmodified", "")}
        return sorted(result.values(), key=lambda x: (x["time"], x["name"]), reverse=True)

    def upload(self, src: Path, name: str, progress=None) -> None:
        validate_name(name)
        url = self.root + quote(name, safe="")
        try:
            with Path(src).open("rb") as source:
                size = Path(src).stat().st_size
                with self._request("PUT", url + ".part", data=_Upload(source, size, progress),
                                   headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"}):
                    pass
            with self._request("MOVE", url + ".part", headers={"Destination": url, "Overwrite": "F"}):
                pass
        except Exception:
            try:
                response = self._request("DELETE", url + ".part", accepted=(404,))
                if response is not None:
                    response.close()
            except Exception:
                pass
            raise

    def download(self, name: str, dst: Path, progress=None) -> None:
        from .crypto import _output
        validate_name(name)
        with self._request("GET", self.root + quote(name, safe="")) as response, _output(dst) as out:
            try:
                total = int(response.headers.get("Content-Length", "0"))
            except ValueError:
                total = 0
            count = 0
            try:
                while chunk := response.read(BLOCK):
                    out.write(chunk)
                    count += len(chunk)
                    if progress:
                        progress(count, total)
            except (OSError, EOFError):
                raise WebDAVError("WebDAV 下载中断，请重试") from None
            if total and count != total:
                raise WebDAVError("WebDAV 下载不完整，请重试")

    def delete(self, name: str) -> None:
        validate_name(name)
        with self._request("DELETE", self.root + quote(name, safe="")):
            pass
