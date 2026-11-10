"""云备份只使用隔离的 KOPITIAM_HOME 和假 WebDAV。"""
import json
import os
import sqlite3
import stat
import threading
import zipfile
from contextlib import closing

import pytest

from kopitiam import backup, config, db
from kopitiam.backup import crypto, store


@pytest.fixture
def configured(dav, monkeypatch):
    monkeypatch.setattr(backup, "other_processes", lambda: [])
    db.init_db()
    store.save(dict(url=dav.url, user="tester", password="application-password", backup_password="备份密钥"))
    return backup.BackupManager()


def finish(manager, kind, **kwargs):
    manager.start(kind, **kwargs)
    manager._thread.join(20)
    assert not manager._thread.is_alive()
    return manager.status()["job"]


def seed(home):
    a = db.claim("codex", cwd=str(home), name="A", pid=os.getpid())
    b = db.claim("claude", cwd=str(home), name="B", pid=os.getpid())
    source = home / "source.txt"
    source.write_text("原附件", encoding="utf-8")
    assert db.Chat().send(a, "备份中的消息", to=["B"], files=[str(source)]).startswith("已发送")
    with db._conn() as c:
        path = c.execute("SELECT path FROM attachments").fetchone()[0]
    return a, b, config.files_dir() / os.path.basename(path)


@pytest.mark.skipif(os.name != "nt", reason="DPAPI 只在 Windows")
def test_settings_dpapi_and_public_secrets(home, dav):
    secret = "一个不会明文保存的密码"
    assert store.unprotect(store.protect(secret)) == secret
    store.save(dict(url=dav.url, user="tester", password=secret, backup_password="backup-secret"))
    raw = (home / "webdav.json").read_text(encoding="utf-8")
    assert secret not in raw and "backup-secret" not in raw
    result = store.save(dict(password="", backup_password="", folder="/nested/kopitiam/"))
    assert result["has_password"] and result["has_backup_password"]
    assert set(result) == {"url", "user", "folder", "has_password", "has_backup_password"}
    assert store.credentials(require_backup=True)["password"] == secret
    assert store.credentials(require_backup=True)["backup_password"] == "backup-secret"
    for values in ({"url": "http://example.org/"}, {"url": "https://user:secret@example.org/"}, {"folder": "../bad"}, {"user": "x:y"}):
        with pytest.raises(ValueError):
            store.save(values)
    assert store.credentials()["password"] == secret
    with pytest.raises(ValueError, match="换了服务器要重新填"):
        store.save({"url": "https://different.example.com/dav/"})
    assert store.public()["url"] == dav.url


def test_backup_restore_roundtrip(configured, home):
    a, b, attachment = seed(home)
    original = attachment.read_bytes()
    job = finish(configured, "backup", with_files=True)
    assert job["state"] == "done", job
    assert configured.status()["last_backup"]["name"] == job["name"]
    db.Chat().send(a, "备份之后的消息", to=["B"])
    attachment.write_bytes(b"changed locally")
    extra = config.files_dir() / "local-extra.txt"
    extra.write_bytes(b"keep")
    result = finish(configured, "restore", name=job["name"])
    assert result["state"] == "done", result
    assert "1 条消息、1 个附件" in result["message"]
    assert "重开各个 agent" in result["message"]
    assert attachment.read_bytes() == original and extra.read_bytes() == b"keep"
    with db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    saved = next(home.glob("restore-saved-*"))
    assert (saved / "files" / attachment.name).read_bytes() == b"changed locally"
    with closing(sqlite3.connect(saved / "chat.db")) as c:
        assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    assert not (home / "tmp").exists()
    assert configured.read_only


@pytest.mark.parametrize("with_files", [True, False])
def test_cross_home_paths_and_exclusions(configured, home, monkeypatch, with_files):
    _, _, attachment = seed(home)
    (config.files_dir() / "subdirectory").mkdir()
    (config.files_dir() / "subdirectory/secret.txt").write_text("not included", encoding="utf-8")
    temp = home / "pack"
    temp.mkdir()
    archive = backup._pack(temp, with_files, lambda *a: None)
    with zipfile.ZipFile(archive) as z:
        assert set(z.namelist()) == {"manifest.json", "chat.db"} | ({"files/" + attachment.name} if with_files else set())
        assert json.loads(z.read("manifest.json"))["with_files"] is with_files
    newhome = home / "new-machine"
    monkeypatch.setenv("KOPITIAM_HOME", str(newhome))
    db.init_db()
    _, files = backup._validate_archive(archive, temp / "checked")
    backup._apply_restore(temp / "checked", files, lambda *a: None)
    with db._conn() as c:
        assert c.execute("SELECT path FROM attachments").fetchone()[0] == str(newhome / "files" / attachment.name)
    assert (newhome / "files" / attachment.name).exists() is with_files


def test_retention_same_host_only(configured, dav):
    client, _ = configured._client()
    client.ensure_dir()
    root = dav.root / "dav/kopitiam"
    for i in range(11):
        (root / f"kopitiam-{backup._host_name()}-20261110-1200{i:02d}.kpbak").write_bytes(b"old")
    other = root / "kopitiam-other-computer-20261110-110000.kpbak"
    other.write_bytes(b"other")
    backup._prune(client)
    assert len(list(root.iterdir())) == 11
    assert other.exists()
    assert not (root / f"kopitiam-{backup._host_name()}-20261110-120000.kpbak").exists()


def test_process_refusal_before_remote_access(configured, monkeypatch, dav):
    monkeypatch.setattr(backup, "other_processes", lambda: ["kopitiam.exe（PID 123）"])
    with pytest.raises(ValueError, match="先关掉.*123"):
        configured.start("restore", name="kopitiam-test.kpbak")
    assert dav.calls == []


def test_busy_lock_across_managers(configured, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = backup._pack
    def wait(*a):
        entered.set()
        assert release.wait(10)
        return original(*a)
    monkeypatch.setattr(backup, "_pack", wait)
    configured.start("backup")
    assert entered.wait(5)
    try:
        for manager in (configured, backup.BackupManager()):
            with pytest.raises(ValueError, match="正在备份"):
                manager.start("backup")
            with pytest.raises(ValueError, match="正在备份"):
                manager.delete("kopitiam-test.kpbak")
    finally:
        release.set()
        configured._thread.join(20)
    assert configured.status()["job"]["state"] == "done"


def test_wrong_password_can_retry(configured, home):
    called = []
    configured.on_restore = lambda: called.append(True)
    seed(home)
    name = finish(configured, "backup")["name"]
    failed = finish(configured, "restore", name=name, backup_password="incorrect")
    assert failed["state"] == "failed" and failed["needs_password"]
    assert configured.read_only is False and called == []
    assert not list(home.glob("restore-saved-*"))
    restored = finish(configured, "restore", name=name, backup_password="备份密钥")
    assert restored["state"] == "done"
    assert "0 个附件" in restored["message"]  # 默认不带附件（seed 里有一个附件）
    assert configured.read_only and called == [True]


def test_download_failure_leaves_services_running(configured, dav):
    called = []
    configured.on_restore = lambda: called.append(True)
    dav.fail["GET"] = 503
    job = finish(configured, "restore", name="kopitiam-test.kpbak")
    assert job["state"] == "failed" and not job["needs_password"]
    assert not configured.read_only and called == []


def make_zip(home, *, extra=None, manifest=None, bad_db=None):
    temp = home / "packed"
    temp.mkdir()
    archive = backup._pack(temp, False, lambda *a: None)
    with zipfile.ZipFile(archive) as z:
        content = {i: z.read(i) for i in z.namelist()}
    if manifest:
        data = json.loads(content["manifest.json"])
        data.update(manifest)
        content["manifest.json"] = json.dumps(data).encode()
    if bad_db is not None:
        content["chat.db"] = bad_db
    with zipfile.ZipFile(archive, "w") as z:
        for name, data in content.items():
            z.writestr(name, data)
        if extra:
            z.writestr(extra, b"bad")
    return archive


@pytest.mark.parametrize("name", ["../escape", "files/../escape", "files/a:b", "files/CON", "files/sub/file", "files/UPPER", "webdav.json", "CHAT.DB"])
def test_archive_rejects_bad_paths(configured, home, name):
    # UPPER 本身合法，但清单说没有附件，也必须拒绝。
    archive = make_zip(home, extra=name)
    with pytest.raises(ValueError):
        backup._validate_archive(archive, home / "checked")
    assert not list(home.glob("restore-saved-*"))


@pytest.mark.parametrize("manifest, error", [({"schema_version": "9999"}, "更新版本"), ({"format": True}, "格式"), ({"app": "other"}, "格式"), ({"files": -1}, "不符"), ({"schema_version": "1"}, "版本不符")])
def test_invalid_manifest(configured, home, manifest, error):
    archive = make_zip(home, manifest=manifest)
    with pytest.raises(ValueError, match=error):
        backup._validate_archive(archive, home / "checked")


def test_reject_invalid_database_and_symlink(configured, home):
    archive = make_zip(home, bad_db=b"not a database")
    with pytest.raises(ValueError, match="校验失败"):
        backup._validate_archive(archive, home / "checked")
    link = zipfile.ZipInfo("files/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "a") as z:
        z.writestr(link, "../outside")
    with pytest.raises(ValueError, match="链接"):
        backup._validate_archive(archive, home / "checked2")


def test_restore_failure_rolls_back(configured, home, monkeypatch):
    a, b, attachment = seed(home)
    temp = home / "packed"
    temp.mkdir()
    archive = backup._pack(temp, True, lambda *a: None)
    _, files = backup._validate_archive(archive, temp / "checked")
    db.Chat().send(a, "必须留下的新消息", to=["B"])
    attachment.write_bytes(b"new content")
    original = backup._copy_atomic
    def fail_source(src, dst):
        if src.is_relative_to(temp):
            raise OSError("injected copy failure")
        original(src, dst)
    monkeypatch.setattr(backup, "_copy_atomic", fail_source)
    with pytest.raises(ValueError, match="原数据已保留"):
        backup._apply_restore(temp / "checked", files, lambda *a: None)
    assert attachment.read_bytes() == b"new content"
    with db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
