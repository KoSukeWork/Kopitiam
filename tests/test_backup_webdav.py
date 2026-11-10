import ssl
import urllib.error

import pytest

from kopitiam.backup.webdav import WebDAV, WebDAVError, validate_folder, validate_name, validate_url


def client(dav):
    return WebDAV(dav.url, "tester", "application-password", "备份/kopitiam")


def test_webdav_stream_upload_list_download_delete(dav, home):
    c = client(dav)
    c.ensure_dir()
    c.ensure_dir()  # MKCOL 已存在也成功
    source = home / "source"
    source.write_bytes(b"test bytes" * 200000)
    name = "kopitiam-电脑-20261110-120000.kpbak"
    progress = []
    c.upload(source, name, lambda n, total: progress.append((n, total)))
    assert progress[-1] == (source.stat().st_size,) * 2
    assert len(progress) > 1
    calls = [(method, path) for method, path, _ in dav.calls]
    assert calls[-2][0] == "PUT" and calls[-2][1].endswith(".part")
    assert calls[-1][0] == "MOVE"
    assert dav.calls[-1][2]["Overwrite"] == "F"
    remote = dav.root / "dav/备份/kopitiam"
    (remote / (name + ".part")).write_bytes(b"partial")
    (remote / "unrelated.txt").write_text("ignore", encoding="utf-8")
    listing = c.list()
    assert [x["name"] for x in listing] == [name]
    assert listing[0]["host"] == "电脑" and listing[0]["time"] == "2026-11-10T12:00:00"
    assert listing[0]["size"] == source.stat().st_size
    assert dav.calls[-1][2]["Depth"] == "1"
    c.download(name, home / "download")
    assert (home / "download").read_bytes() == source.read_bytes()
    c.delete(name)
    assert c.list() == []


def test_move_failure_removes_partial_without_overwriting(dav, home):
    c = client(dav)
    c.ensure_dir()
    source = home / "file"
    source.write_bytes(b"original")
    name = "kopitiam-PC-20261110-120000.kpbak"
    c.upload(source, name)
    source.write_bytes(b"different")
    with pytest.raises(WebDAVError, match="412"):
        c.upload(source, name)
    assert (dav.root / "dav/备份/kopitiam" / name).read_bytes() == b"original"
    assert not (dav.root / "dav/备份/kopitiam" / (name + ".part")).exists()


@pytest.mark.parametrize("code, text", [(401, "应用密码"), (403, "拒绝访问"), (404, "找不到"), (405, "不支持"), (507, "空间不够")])
def test_webdav_http_errors(dav, code, text):
    dav.fail["PROPFIND"] = code
    with pytest.raises(WebDAVError, match=text) as e:
        client(dav).list()
    assert str(code) in str(e.value)


def test_wrong_credentials_and_redirect_do_not_leak(dav, home):
    c = WebDAV(dav.url, "tester", "wrong")
    with pytest.raises(WebDAVError, match="401"):
        c.list()
    # localhost 是不同 hostname，绝不把认证头跟过去。
    dav.redirect = dav.url.replace("127.0.0.1", "localhost") + "other"
    before = len(dav.calls)
    with pytest.raises(WebDAVError, match="重定向"):
        client(dav).download("kopitiam-PC-20261110-120000.kpbak", home / "out")
    assert len(dav.calls) == before + 1
    assert not (home / "out").exists()


@pytest.mark.parametrize("reason, message", [(TimeoutError(), "超时"), (ssl.SSLCertVerificationError(), "证书")])
def test_network_errors(dav, monkeypatch, reason, message):
    c = client(dav)
    def fail(*args, **kwargs):
        raise urllib.error.URLError(reason)
    monkeypatch.setattr(c.opener, "open", fail)
    with pytest.raises(WebDAVError, match=message):
        c.list()


@pytest.mark.parametrize("url", ["http://example.com/dav", "https://user:secret@example.com/", "ftp://example.com/", "https://example.com/?token=x", "https://example.com/#x", "https://example.com:0/"])
def test_url_validation(url):
    with pytest.raises(WebDAVError):
        validate_url(url)


def test_local_url_and_folder_validation():
    assert validate_url("http://[::1]:8888/dav") == "http://[::1]:8888/dav/"
    assert validate_folder("/backups/kopitiam/") == "backups/kopitiam"
    for folder in ("../evil", "a/../b", "a..b", "a\\b", "a//b", "a/./b"):
        with pytest.raises(WebDAVError):
            validate_folder(folder)
    for name in ("../kopitiam-a.kpbak", "kopitiam-a.kpbak.part", "/kopitiam-a.kpbak", "kopitiam-a.kpbak\n"):
        with pytest.raises(WebDAVError):
            validate_name(name)
