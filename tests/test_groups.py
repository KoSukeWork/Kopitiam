"""群聊收件人、路由和迁移：同一套规则供 MCP 和网页使用。"""
import json
import os

import pytest

from kopitiam import db


@pytest.fixture
def agents(home):
    return [db.claim(kind, cwd=str(home), name=name, pid=os.getpid())
            for kind, name in (("claude", "frontend"), ("codex", "codex"), ("codex", "codex@web"), ("pi", "outsider"))]


def group(*members, title="订单分页"):
    uid = db.user_id()
    with db._conn() as c:
        gid = c.execute("INSERT INTO groups(title, about, created_at) VALUES(?, '接上分页参数', ?)",
                        (title, db.now_iso())).lastrowid
        db._group_event(c, gid, uid, "created")
        for aid in members:
            db._add_member(c, gid, aid, uid)
    return gid


def last():
    with db._conn() as c:
        return c.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 1").fetchone()


@pytest.mark.parametrize("text, expected", [
    ("@codex@web你好", [2]), ("@CoDeX@WeB", [2]), ("@codex 好", [1]),
    ("claude@Client a@b.com", []), ("@frontend", []), ("@user", [4]),
    ("@nobody", []), ("(@codex@web) @codex@web", [2]),
])
def test_mentions(text, expected):
    candidates = [{"id": i, "name": n} for i, n in enumerate(["frontend", "codex", "codex@web", "Client", "user"])]
    assert db.parse_mentions(text, candidates, 0) == expected


def test_group_send_routing_and_old_visibility(agents):
    a, b, d, outsider = agents
    chat = db.Chat()
    g = group(a, b, d)
    assert f"群聊 {g}" in chat.send(a, "hello", group=g)
    assert set(json.loads(last()["to_ids"])) == {b, d, db.user_id()}
    assert last()["to_all"] == 1
    # 没有 to / group，且只在一个进行中的群里。
    chat.send(a, "implicit")
    assert last()["group_id"] == g
    # 显式 to（包括空列表）始终是群以外。
    chat.send(a, "direct", to=["outsider"])
    assert last()["group_id"] is None
    chat.send(a, "broadcast", to=[])
    assert last()["group_id"] is None
    g2 = group(a, b)
    chat.send(a, "ambiguous")
    assert last()["group_id"] is None
    chat.send(b, "@frontend group context", group=g2)
    chat.read(a)
    chat.send(a, "last read group")
    assert last()["group_id"] == g2
    chat.send(b, "@frontend private in group", group=g)
    m = last()
    for aid in agents:
        old_visible = not json.loads(m["to_ids"]) or aid in json.loads(m["to_ids"])
        assert db.Chat._visible(m["to_ids"], aid) == old_visible
    assert not db.Chat._visible(m["to_ids"], outsider)


def test_errors_are_atomic_and_auto_invite(agents):
    a, b, d, outsider = agents
    g = group(a)
    chat = db.Chat()
    for kwargs in ({"group": g, "to": []}, {"group": 999}):
        assert chat.send(a, "bad", **kwargs).startswith("错误：")
    assert chat.send(b, "bad", group=g).startswith("错误：")
    assert last() is None
    assert "已把 codex@web 拉进群聊" in chat.send(a, "@codex@web你好", group=g)
    assert json.loads(last()["to_ids"]) == [d]
    with db._conn() as c:
        assert db._group_member(c, g, d)
        assert c.execute("SELECT actor_id FROM group_events WHERE group_id=? AND agent_id=?", (g, d)).fetchone()[0] == a
    chat.archive("codex")
    chat.send(a, "@codex hello", group=g)
    assert last()["to_all"] == 1
    with db._conn() as c:
        assert not db._group_member(c, g, b)
        c.execute("UPDATE groups SET archived_at=? WHERE id=?", (db.now_iso(), g))
    before = last()["id"]
    assert "已归档" in chat.send(a, "bad", group=g)
    assert last()["id"] == before
    chat.send(a, "fallback")
    assert last()["group_id"] is None


def test_read_intro_context_history_and_former_members(agents):
    a, b, d, outsider = agents
    g = group(a, b, d)
    chat = db.Chat()
    chat.send(a, "@codex@web only", group=g)
    assert chat.read(b) == "没有新消息。"
    assert "only" in chat.history(b, group=g)
    assert chat.history(outsider, group=g).startswith("错误：")
    chat.send(a, "all", group=g)
    first = chat.read(b)
    assert "【群聊" in first and "接上分页参数" in first and "only" not in first
    assert f"history(group={g})" in first
    chat.send(a, "all again", group=g)
    assert "【群聊" not in chat.read(b)
    assert "（4 人）" in chat.whoami(b)
    chat.send(a, "direct", to=["codex"])
    chat.read(b)
    with db._conn() as c:
        assert c.execute("SELECT last_group FROM agents WHERE id=?", (b,)).fetchone()[0] is None
        c.execute("DELETE FROM group_members WHERE group_id=? AND agent_id=?", (g, b))
    assert "only" in chat.history(b, group=g)


def test_nonempty_targets_and_purge_preserve_group(agents):
    a, b, d, outsider = agents
    chat = db.Chat()
    g = group(a)
    chat.send(a, "@frontend", group=g)
    assert json.loads(last()["to_ids"]) == [db.user_id()]
    empty = group()
    chat.send(db.user_id(), "empty room", group=empty)
    assert json.loads(last()["to_ids"]) == [db.user_id()]
    chat.send(a, "@codex", group=g)
    chat.trash("codex")
    assert "已永久删除" in chat.purge("codex")
    assert json.loads(last()["to_ids"]) == [db.user_id()]
    with db._conn() as c:
        assert not db._group_member(c, g, b)
        assert c.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 2
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
    chat.send(a, "still works", group=g)
    assert last()["group_id"] == g


def test_archived_unread_still_delivered(agents):
    a, b, *_ = agents
    g = group(a, b)
    db.Chat().send(a, "pending", group=g)
    with db._conn() as c:
        c.execute("UPDATE groups SET archived_at=? WHERE id=?", (db.now_iso(), g))
    assert "pending" in db.Chat().read(b)


def test_remote_cannot_be_invited_and_failed_send_rolls_back_invite(agents):
    a, b, *_ = agents
    peer = db.add_peer("remote", "remote", "example:123", "secret")
    g = group(a)
    chat = db.Chat()
    assert chat.send(a, "@codex", group=g, files=["does-not-exist"]).startswith("错误：")
    with db._conn() as c:
        assert not db._group_member(c, g, b)
    chat.send(a, "@remote hello", group=g)
    assert peer["agent_id"] not in json.loads(last()["to_ids"])


def test_migration_preserves_legacy_messages(agents):
    a, b, *_ = agents
    db.Chat().send(a, "legacy", to=["codex"])
    with db._conn() as c:
        c.execute("DROP INDEX messages_group")
        c.execute("ALTER TABLE messages DROP COLUMN group_id")
        c.execute("ALTER TABLE messages DROP COLUMN to_all")
        c.execute("ALTER TABLE agents DROP COLUMN last_group")
        c.execute("UPDATE meta SET value='10' WHERE key='schema_version'")
    db.init_db()
    db.init_db()
    assert db.get_meta("schema_version") == "11"
    assert last()["group_id"] is None and last()["to_all"] is None
    assert "legacy" in db.Chat().read(b)
