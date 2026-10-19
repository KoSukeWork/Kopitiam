"""SQLite 存储层：建表、连接、身份认领、Chat 业务逻辑。

业务逻辑都放在 Chat 里（第一个业务参数是 me: int，即 agent id），MCP 工具和网页都是薄包装。
所有时间戳用本地时间 ISO 字符串（精确到秒）；消息里只存 sender_id，显示时再取当前名字。
写操作都放在 BEGIN IMMEDIATE ... COMMIT 里，方法失败返回以「错误：」开头的文本，不抛异常
（身份认领 claim 除外——它在进程启动时调用，失败要直接退出）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import psutil

from .config import (
    FILE_MAX,
    FILES_MAX,
    FORWARD_EXTRA,
    PEER_ONLINE_WINDOW,
    PURGE_AFTER,
    TEXT_MAX,
    WHO_WINDOW,
    data_dir,
    db_path,
    files_dir,
    norm,
    safe,
    valid_name,
)

SCHEMA_VERSION = "4"  # 2：团队（peers / outbox / messages.remote_uid）；3：自动唤醒（wake_state）；4：agents.thread_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS agents(
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
  kind       TEXT NOT NULL,
  cwd        TEXT NOT NULL,
  launch_cwd TEXT NOT NULL,
  pid        INTEGER,
  pid_ctime  REAL,
  last_read  INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  last_seen  TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  sender_id  INTEGER NOT NULL REFERENCES agents(id),
  to_ids     TEXT NOT NULL,
  text       TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attachments(
  id         INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id),
  path       TEXT NOT NULL,
  orig_name  TEXT NOT NULL,
  size       INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS peers(
  id         INTEGER PRIMARY KEY,
  node_id    TEXT NOT NULL UNIQUE,
  name       TEXT NOT NULL,
  address    TEXT NOT NULL,
  secret     TEXT NOT NULL,
  agent_id   INTEGER NOT NULL REFERENCES agents(id),
  created_at TEXT NOT NULL,
  last_ok    REAL);
CREATE TABLE IF NOT EXISTS outbox(
  id         INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id),
  peer_id    INTEGER NOT NULL REFERENCES peers(id),
  status     TEXT NOT NULL DEFAULT 'pending',
  tries      INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  next_try   REAL NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS wake_state(
  agent_id    INTEGER PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
  enabled     INTEGER NOT NULL DEFAULT 1,
  seen_upto   INTEGER NOT NULL DEFAULT 0,
  woken_upto  INTEGER NOT NULL DEFAULT 0,
  woken_at    REAL,
  note        TEXT);
"""
# peers：配对过的别人的电脑。name = 对方自报的名字；address = 对方团队端口 host:port；
#   secret = 配对时生成的共享密钥（HMAC 签名用）；agent_id = 本机为这个人建的身份（kind='remote'）。
# outbox：发给远端的人的消息，后台线程负责投递；status = pending / sent。
# messages.remote_uid：别人发来的消息的全局 id（「对方 node_id:对方消息 id」），用来去重。
# agents.thread_id：最近一次调用 kopitiam 工具的对话 ID（Codex 在 _meta.threadId 里带上；其他 agent 为空）。
#   一个 Codex 进程管着好几个对话，靠它知道自动唤醒、导出记录该找哪个对话。
# wake_state：自动唤醒的进度（只有感知模块读写，见 wake/detector.py）。seen_upto = 这个 id 及以前的消息
#   已经判断过要不要叫醒；woken_upto = 最近一次叫醒时包含的最大消息 id（agent 读到这里之前不重复叫）；
#   note = 最近一次的结果，给网页显示；enabled = 用户在网页上关掉某个 agent 的自动唤醒时为 0。


class _Fail(Exception):
    """业务失败；str(e) 就是返回给调用者的文本（已带「错误：」前缀）。"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    """新连接。每个线程用自己的连接；调用方负责 close。"""
    data_dir().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _conn():
    c = connect()
    try:
        yield c
    finally:
        c.close()


def init_db() -> None:
    files_dir().mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.executescript(SCHEMA)
        c.execute("BEGIN IMMEDIATE")
        try:
            # v1 → v2：messages 加 remote_uid 列；v3 → v4：agents 加 thread_id 列
            # （几个进程可能同时启动，重复加列的错误忽略）
            for table, col in (("messages", "remote_uid"), ("agents", "thread_id")):
                if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
                    try:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                    except sqlite3.OperationalError as e:
                        if "duplicate column" not in str(e):
                            raise
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_remote_uid ON messages(remote_uid)")
            c.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SCHEMA_VERSION,),
            )
            c.execute(
                "INSERT OR IGNORE INTO agents(name, kind, cwd, launch_cwd, pid, pid_ctime,"
                " last_read, created_at, last_seen) VALUES('user', 'human', ?, '', NULL, NULL, 0, ?, ?)",
                (str(Path.home()), now_iso(), now_iso()),
            )
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")


def get_meta(key: str, default: str | None = None) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def node_id() -> str:
    """这台电脑的 kopitiam 的唯一 id（第一次用时生成，存在 meta 里）。"""
    nid = get_meta("node_id")
    if nid is None:
        with _conn() as c:
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('node_id', ?)", (uuid.uuid4().hex,))
        nid = get_meta("node_id")
    return nid


def display_name() -> str:
    """团队里别人看到的我的名字；默认 Windows 用户名。"""
    return get_meta("display_name") or os.environ.get("USERNAME") or socket.gethostname() or "me"


def set_display_name(name: str) -> str:
    name = (name or "").strip()
    if not valid_name(name) or name.lower() == "user":
        return f"错误：名字不合法：{name}（1-32 个字符，字母/数字/下划线/中文/./@/-）"
    set_meta("display_name", name)
    return f"已设置：别人会看到你叫 {name}"


def is_online(row) -> bool:
    """pid 在、进程活着、且 ctime 对得上才算在线；任何 psutil 错误都当离线。"""
    pid = row["pid"]
    if pid is None:
        return False
    try:
        if not psutil.pid_exists(pid):
            return False
        return abs(psutil.Process(pid).create_time() - (row["pid_ctime"] or 0.0)) < 1
    except psutil.Error:
        return False


def _pid_ctime(pid: int) -> float:
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return 0.0  # 查不到的 pid，is_online 会当离线


def _insert_agent(c, *, name: str, kind: str, cwd: str, launch_cwd: str,
                  pid: int, pid_ctime: float) -> int:
    # 新 agent 的 last_read = 当前最大消息 id：不会收到很久以前的旧消息
    last_read = c.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
    cur = c.execute(
        "INSERT INTO agents(name, kind, cwd, launch_cwd, pid, pid_ctime, last_read, created_at, last_seen)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, kind, cwd, launch_cwd, pid, pid_ctime, last_read, now_iso(), now_iso()),
    )
    return int(cur.lastrowid)


def _name_taken(c, name: str) -> bool:
    return c.execute("SELECT 1 FROM agents WHERE name = ?", (name,)).fetchone() is not None


def _all_names(c) -> str:
    """给错误提示用的现有名字列表（用户排在最后，和 who 的习惯一致）。"""
    rows = c.execute("SELECT name, kind FROM agents ORDER BY id").fetchall()
    names = [r["name"] for r in rows if r["kind"] != "human"]
    names += [r["name"] for r in rows if r["kind"] == "human"]
    return ", ".join(names)


def _check_name(name: str) -> None:
    if not valid_name(name):
        raise ValueError(
            f"错误：名字不合法：{name}（1-32 个字符，字母/数字/下划线/中文/./@/-，不能是 all 或 所有人）"
        )
    # NOTE: spec 只保留 all/所有人；'user' 是网页的固定身份，也必须保留，
    # 否则 agent 可以认领或改名成 user 抢走网页身份。
    if name.lower() == "user":
        raise ValueError("错误：user 是网页保留身份，不能使用")


_LAUNCHER_NAMES = {"kopitiam.exe", "kopitiam", "python.exe", "python", "pythonw.exe", "py.exe", "uv.exe", "uvx.exe"}


def host_pid() -> int:
    """这个 MCP 进程的「主人」：agent 本身的进程（claude.exe / codex.exe / node.exe…）。

    agent 启动 kopitiam.exe，它再经过 python 启动器才到这里，所以要往上跳过这一串。
    只跳过命令行里带 kopitiam 的 python，免得把用 Python 写的 agent 也跳过去。
    """
    try:
        me = psutil.Process(os.getpid())
        for p in me.parents():
            name = p.name().lower()
            if name in ("kopitiam.exe", "kopitiam"):
                continue
            if name in _LAUNCHER_NAMES:
                try:
                    if "kopitiam" in " ".join(p.cmdline()).lower():
                        continue
                except psutil.Error:
                    pass
            return p.pid
    except psutil.Error:
        pass
    return os.getpid()


def _same_host(row, pid: int) -> bool:
    """这一行正被同一个 agent 进程占着（它又开了一个 MCP 进程）：共用这个身份，不另起 -2。"""
    return row["pid"] == pid and is_online(row)


def _usable(row, pid: int) -> bool:
    # 团队里别人的身份（kind=remote）不能被本机 agent 认领，当作名字已占用
    return row["kind"] != "remote" and (not is_online(row) or _same_host(row, pid))


def _drop_spare_names(c, name: str, keep: int) -> None:
    """认领到 name 以后，删掉离线、没有消息的 name-2、name-3……（以前多开时留下的空身份）。"""
    counts = _message_counts(c)
    for r in c.execute("SELECT * FROM agents WHERE name LIKE ? AND id <> ?", (name + "-%", keep)).fetchall():
        # LIKE 里的 _ 是通配符，所以再精确核对一遍前缀
        if not r["name"].lower().startswith(name.lower() + "-"):
            continue
        suffix = r["name"][len(name) + 1:]
        if suffix.isdigit() and r["kind"] != "remote" and not counts.get(r["id"]) and not is_online(r):
            c.execute("DELETE FROM agents WHERE id=?", (r["id"],))


def claim(kind: str, cwd: str | None = None, *, name: str | None = None,
          pid: int | None = None, pid_ctime: float | None = None) -> int:
    """认领身份，返回 agent id。cwd 默认当前进程目录（MCP 启动时的 os.getcwd()）。

    pid 默认是 agent 本身的进程（见 host_pid）：同一个 agent 开好几个 MCP 进程时（Codex 桌面版
    会这样，dsh 也会先开一个探测进程）共用一个身份；agent 开着就算在线。
    """
    if cwd is None:
        cwd = os.getcwd()
    cwd = str(cwd)
    if pid is None:
        pid = host_pid()
    if pid_ctime is None:
        pid_ctime = _pid_ctime(pid)
    if name is not None:
        _check_name(name)
    init_db()
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            if name is not None:
                row = c.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
                if row is not None and _usable(row, pid):
                    # 认领这一行：写 pid、pid_ctime、last_seen，kind 也更新
                    c.execute(
                        "UPDATE agents SET pid=?, pid_ctime=?, last_seen=?, kind=? WHERE id=?",
                        (pid, pid_ctime, now_iso(), kind, row["id"]),
                    )
                    _drop_spare_names(c, name, row["id"])
                    c.execute("COMMIT")
                    return int(row["id"])
                if row is not None:
                    # 名字被别的 agent 占着：依次看 名字-2、名字-3……
                    # 已存在但离线的就认领（否则每次重启都会多出一行），都在线才新建
                    i = 2
                    while True:
                        alt = c.execute("SELECT * FROM agents WHERE name = ?", (f"{name}-{i}",)).fetchone()
                        if alt is None:
                            break
                        if _usable(alt, pid):
                            c.execute(
                                "UPDATE agents SET pid=?, pid_ctime=?, last_seen=?, kind=? WHERE id=?",
                                (pid, pid_ctime, now_iso(), kind, alt["id"]),
                            )
                            c.execute("COMMIT")
                            return int(alt["id"])
                        i += 1
                    name = f"{name}-{i}"
                agent_id = _insert_agent(c, name=name, kind=kind, cwd=cwd,
                                         launch_cwd=cwd, pid=pid, pid_ctime=pid_ctime)
                c.execute("COMMIT")
                return agent_id

            # 没给 name：找 kind 相同、launch_cwd 相同、能用的行（离线，或正被同一个 agent 占着）
            candidates = c.execute(
                "SELECT * FROM agents WHERE kind=? ORDER BY last_seen DESC", (kind,)
            ).fetchall()
            for cand in sorted(candidates, key=lambda r: not _same_host(r, pid)):  # 同一个 agent 的优先
                if norm(cand["launch_cwd"]) == norm(cwd) and _usable(cand, pid):
                    c.execute(
                        "UPDATE agents SET pid=?, pid_ctime=?, last_seen=? WHERE id=?",
                        (pid, pid_ctime, now_iso(), cand["id"]),
                    )
                    c.execute("COMMIT")
                    return int(cand["id"])
            # 找不到：新建，名字用 kind@目录名，重名加 -2、-3
            base = f"{kind}@{Path(cwd).name or 'root'}"
            nm = base
            i = 2
            while _name_taken(c, nm):
                nm = f"{base}-{i}"
                i += 1
            agent_id = _insert_agent(c, name=nm, kind=kind, cwd=cwd,
                                     launch_cwd=cwd, pid=pid, pid_ctime=pid_ctime)
            c.execute("COMMIT")
            return agent_id
        except BaseException:
            c.execute("ROLLBACK")
            raise


def _other_mcp_alive(host: int) -> bool:
    """同一个 agent 下面，除了我（和我的启动器）以外，还有没有别的 kopitiam MCP 进程。"""
    try:
        me = psutil.Process(os.getpid())
        mine = {me.pid} | {p.pid for p in me.parents()}
        for ch in psutil.Process(host).children(recursive=True):
            if ch.pid in mine:
                continue
            try:
                cmd = " ".join(ch.cmdline()).lower()
            except psutil.Error:
                continue
            if "kopitiam" in cmd and " mcp" in cmd:
                return True
    except psutil.Error:
        pass
    return False


def release(agent_id: int, host: int | None = None) -> None:
    """进程退出时把自己这一行标成离线（atexit 调；崩溃的进程靠 is_online 判断）。

    给了 host（agent 本身的进程）时：同一个 agent 还有别的 MCP 进程在跑就不动；
    并且只在这一行还归这个 agent 时才清（期间被别的 agent 认领了就不碰）。
    """
    try:
        if host is not None and _other_mcp_alive(host):
            return
        with _conn() as c:
            if host is None:
                c.execute("UPDATE agents SET pid=NULL, pid_ctime=NULL WHERE id=?", (agent_id,))
            else:
                c.execute("UPDATE agents SET pid=NULL, pid_ctime=NULL WHERE id=? AND pid=?", (agent_id, host))
    except Exception:
        pass  # 退出清理尽力而为


def set_thread(agent_id: int, thread_id: str) -> None:
    """记下这个 agent 最近一次调用 kopitiam 的对话 ID（只有 Codex 会带，见 agents.thread_id）。"""
    with _conn() as c:
        c.execute("UPDATE agents SET thread_id=? WHERE id=?", (thread_id, agent_id))


def _ago_cutoff(seconds: int) -> str:
    return (datetime.now() - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _is_hidden(row, cutoff: str, peer_agents: set[int] = frozenset()) -> bool:
    """离线超过 WHO_WINDOW 的 agent：不在 who / 网页主列表里显示（用户永远显示）。

    团队里的人（kind=remote）：还在配对就不隐藏；解除配对以后马上隐藏。
    """
    if row["kind"] == "human":
        return False
    if row["kind"] == "remote":
        return row["id"] not in peer_agents
    return row["last_seen"] < cutoff and not is_online(row)


def _peer_agent_ids(c) -> set[int]:
    return {r[0] for r in c.execute("SELECT agent_id FROM peers")}


def _message_counts(c) -> dict[int, int]:
    """每个 agent 关联的消息数：它发的 + 单独发给它的（广播不算）。"""
    counts: dict[int, int] = {}
    for sid, n in c.execute("SELECT sender_id, COUNT(*) FROM messages GROUP BY sender_id"):
        counts[sid] = counts.get(sid, 0) + n
    for (t,) in c.execute("SELECT to_ids FROM messages WHERE to_ids <> '[]'"):
        for i in set(Chat._targets(t)):
            counts[i] = counts.get(i, 0) + 1
    return counts


def purge_stale() -> int:
    """删掉离线超过 PURGE_AFTER、而且没有任何消息关联的身份，返回删掉的个数。

    有消息关联的身份只隐藏不删：历史消息要靠它显示发送者名字（外键也不允许删）。
    改过名的身份一定发过改名广播，所以自定义名字不会被清理掉。
    """
    cutoff = _ago_cutoff(PURGE_AFTER)
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            counts = _message_counts(c)
            paired = _peer_agent_ids(c)  # 还在配对的同事不清理
            rows = c.execute(
                "SELECT * FROM agents WHERE kind <> 'human' AND last_seen < ?", (cutoff,)
            ).fetchall()
            gone = [r["id"] for r in rows
                    if not counts.get(r["id"]) and r["id"] not in paired and not is_online(r)]
            for i in gone:
                c.execute("DELETE FROM agents WHERE id=?", (i,))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
    return len(gone)


# ---- 团队：配对过的别人的电脑（人和人之间互发消息，agent 不直接参与） ----
# 远端的人在本机是一个 kind='remote' 的身份：cwd 存对方地址，launch_cwd 存对方 node_id
# （解除配对后再配对，靠它找回原来的身份和历史消息）。

def _alias_for(c, name: str) -> str:
    """给远端的人在本机取一个不冲突的名字：用对方自报的名字，重名就加 -2、-3。"""
    base = name if valid_name(name) and name.lower() != "user" else "peer"
    nm, i = base, 2
    while _name_taken(c, nm):
        nm, i = f"{base}-{i}", i + 1
    return nm


def add_peer(peer_node: str, name: str, address: str, secret: str) -> dict:
    """配对成功后保存对方；同一台电脑重新配对时只更新密钥/地址/名字，沿用原来的本机身份。"""
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT * FROM peers WHERE node_id=?", (peer_node,)).fetchone()
            if row is not None:
                c.execute("UPDATE peers SET name=?, address=?, secret=?, last_ok=? WHERE id=?",
                          (name, address, secret, time.time(), row["id"]))
                agent_id = row["agent_id"]
            else:
                old = c.execute("SELECT id FROM agents WHERE kind='remote' AND launch_cwd=?",
                                (peer_node,)).fetchone()
                agent_id = old["id"] if old else _insert_agent(
                    c, name=_alias_for(c, name), kind="remote", cwd=address,
                    launch_cwd=peer_node, pid=None, pid_ctime=None)
                c.execute(
                    "INSERT INTO peers(node_id, name, address, secret, agent_id, created_at, last_ok)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (peer_node, name, address, secret, agent_id, now_iso(), time.time()),
                )
            c.execute("UPDATE agents SET cwd=?, last_seen=? WHERE id=?", (address, now_iso(), agent_id))
            alias = c.execute("SELECT name FROM agents WHERE id=?", (agent_id,)).fetchone()["name"]
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
    return {"alias": alias, "agent_id": agent_id}


_PEER_SELECT = "SELECT p.*, a.name AS alias FROM peers p JOIN agents a ON a.id = p.agent_id"


def list_peers() -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(_PEER_SELECT + " ORDER BY p.id").fetchall()


def peer_by_node(peer_node: str):
    with _conn() as c:
        return c.execute(_PEER_SELECT + " WHERE p.node_id=?", (peer_node,)).fetchone()


def peer_by_alias(alias: str):
    with _conn() as c:
        return c.execute(_PEER_SELECT + " WHERE a.name=?", (alias,)).fetchone()


def peer_online(row) -> bool:
    return (row["last_ok"] or 0) >= time.time() - PEER_ONLINE_WINDOW


def peer_seen(peer_id: int, *, address: str | None = None, name: str | None = None) -> None:
    """成功联系到对方：记下时间（和对方的新地址/新名字）；对方刚上线时，让排队的消息马上重发。"""
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT * FROM peers WHERE id=?", (peer_id,)).fetchone()
            if row is not None:
                if not peer_online(row):
                    c.execute("UPDATE outbox SET next_try=0 WHERE peer_id=? AND status='pending'", (peer_id,))
                c.execute("UPDATE peers SET last_ok=?, address=COALESCE(?, address), name=COALESCE(?, name)"
                          " WHERE id=?", (time.time(), address, name, peer_id))
                c.execute("UPDATE agents SET last_seen=?, cwd=COALESCE(?, cwd) WHERE id=?",
                          (now_iso(), address, row["agent_id"]))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def remove_peer(peer_id: int) -> None:
    """解除配对：删掉配对信息和还没送出的消息。本机身份保留（历史消息要显示名字），收进「已隐藏」。"""
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("DELETE FROM outbox WHERE peer_id=?", (peer_id,))
            c.execute("DELETE FROM peers WHERE id=?", (peer_id,))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def due_outbox(limit: int = 20) -> list[dict]:
    """到时间该投递（或重试）的发件箱条目，带上消息正文和附件。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT o.id AS ob_id, o.tries, o.message_id, p.id AS peer_id, p.address, p.secret, m.text"
            " FROM outbox o JOIN peers p ON p.id = o.peer_id JOIN messages m ON m.id = o.message_id"
            " WHERE o.status='pending' AND o.next_try <= ? ORDER BY o.id LIMIT ?",
            (time.time(), limit),
        ).fetchall()
        out = []
        for r in rows:
            atts = c.execute("SELECT path, orig_name, size FROM attachments WHERE message_id=? ORDER BY id",
                             (r["message_id"],)).fetchall()
            out.append(dict(r) | {"files": [dict(a) for a in atts]})
    return out


def outbox_done(ob_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE outbox SET status='sent', last_error=NULL, updated_at=? WHERE id=?",
                  (time.time(), ob_id))


def outbox_retry(ob_id: int, tries: int, error: str) -> None:
    """投递失败：记下原因，按 3s、6s、12s……最长 60s 的间隔重试。"""
    wait = min(60, 3 * 2 ** min(tries, 5))
    with _conn() as c:
        c.execute("UPDATE outbox SET tries=tries+1, last_error=?, next_try=?, updated_at=? WHERE id=?",
                  (error[:300], time.time() + wait, time.time(), ob_id))


def receive_remote(peer_node: str, uid: str, text: str, files: list[tuple[Path, str]]) -> int | None:
    """存一条别人发来的消息（只发给本机 user，agent 看不到）。uid 重复（对方重发）就忽略，返回 None。

    files：已经在 staging 里校验过大小和哈希的 (临时文件, 原文件名)，成功后移进 FILES_DIR。
    """
    moved: list[Path] = []
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            peer = c.execute("SELECT * FROM peers WHERE node_id=?", (peer_node,)).fetchone()
            if peer is None:
                raise LookupError("unknown peer")
            if c.execute("SELECT 1 FROM messages WHERE remote_uid=?", (uid,)).fetchone():
                c.execute("ROLLBACK")
                for tmp, _ in files:
                    tmp.unlink(missing_ok=True)
                return None
            me = c.execute("SELECT id FROM agents WHERE name='user'").fetchone()["id"]
            cur = c.execute(
                "INSERT INTO messages(sender_id, to_ids, text, created_at, remote_uid) VALUES(?, ?, ?, ?, ?)",
                (peer["agent_id"], json.dumps([me]), text, now_iso(), uid),
            )
            mid = int(cur.lastrowid)
            files_dir().mkdir(parents=True, exist_ok=True)
            for i, (tmp, name) in enumerate(files):
                dest = files_dir() / f"remote_{mid:06d}_{i}_{safe(name)}"
                shutil.move(tmp, dest)
                moved.append(dest)
                c.execute("INSERT INTO attachments(message_id, path, orig_name, size) VALUES(?, ?, ?, ?)",
                          (mid, str(dest), name, dest.stat().st_size))
            c.execute("UPDATE agents SET last_seen=? WHERE id=?", (now_iso(), peer["agent_id"]))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            for d in moved:
                d.unlink(missing_ok=True)
            raise
    return mid


def _delivery(c, where: str, params) -> dict[int, dict]:
    """每条消息的投递状态：全部送达 = sent，否则 pending（带最近一次失败原因）。"""
    out: dict[int, dict] = {}
    for r in c.execute(f"SELECT message_id, status, last_error FROM outbox WHERE {where}", params):
        d = out.setdefault(r["message_id"], {"state": "sent", "error": None})
        if r["status"] != "sent":
            d["state"] = "pending"
            d["error"] = d["error"] or r["last_error"]
    return out


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def user_id() -> int:
    init_db()
    with _conn() as c:
        row = c.execute("SELECT id FROM agents WHERE name='user'").fetchone()
    if row is None:
        raise RuntimeError("user 身份不存在")
    return int(row["id"])


def _ago(iso: str) -> str:
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return "未知时间"
    s = max(0, int((datetime.now() - t).total_seconds()))
    if s < 60:
        return "刚刚"
    if s < 3600:
        return f"{s // 60} 分钟前"
    if s < 86400:
        return f"{s // 3600} 小时前"
    return f"{s // 86400} 天前"


def _fmt_time(iso: str) -> str:
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if t.date() == datetime.now().date():
        return t.strftime("%H:%M")
    return t.strftime("%m-%d %H:%M")


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{f'{n / 1024:.1f}'.rstrip('0').rstrip('.')} KB"
    return f"{f'{n / 1024 / 1024:.1f}'.rstrip('0').rstrip('.')} MB"


class Chat:
    """全部业务逻辑；方法的第一个业务参数是 me（agent id）。"""

    # ---- 内部工具 ----

    @staticmethod
    def _me(c, me: int) -> sqlite3.Row:
        row = c.execute("SELECT * FROM agents WHERE id=?", (me,)).fetchone()
        if row is None:
            raise LookupError(f"agent id {me} 不存在")
        return row

    @staticmethod
    def _touch(c, me: int) -> None:
        c.execute("UPDATE agents SET last_seen=? WHERE id=?", (now_iso(), me))

    @staticmethod
    def _targets(to_ids_json: str) -> list[int]:
        try:
            v = json.loads(to_ids_json)
            return v if isinstance(v, list) else []
        except (TypeError, ValueError):
            return []

    @staticmethod
    def _visible(to_ids_json: str, me: int) -> bool:
        t = Chat._targets(to_ids_json)
        return t == [] or me in t

    # ---- 6.1 whoami ----

    def whoami(self, me: int) -> str:
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                me_row = self._me(c, me)
                self._touch(c, me)
                last_read = me_row["last_read"]
                rows = c.execute(
                    "SELECT id, to_ids FROM messages WHERE id > ? AND sender_id <> ?",
                    (last_read, me),
                ).fetchall()
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        unread = sum(1 for r in rows if self._visible(r["to_ids"], me))
        return (
            f"你是 {me_row['name']}（{me_row['kind']}，工作目录 {me_row['cwd']}）。"
            f"未读消息 {unread} 条。"
        )

    # ---- 6.2 who ----

    def who(self) -> str:
        with _conn() as c:
            rows = c.execute("SELECT * FROM agents").fetchall()
        cutoff = _ago_cutoff(WHO_WINDOW)
        # 在线的永远显示（哪怕很久没调用工具，last_seen 很旧）；团队里别人电脑上的人不列出：
        # agent 不能直接发给他们，只能由用户在网页上转发
        shown = [r for r in rows if r["kind"] != "remote" and not _is_hidden(r, cutoff)]
        shown.sort(key=lambda r: r["last_seen"], reverse=True)
        shown.sort(key=lambda r: 0 if (r["name"] == "user" or is_online(r)) else 1)
        if not shown:
            return "（还没有其他 agent）"
        w_name = max(len(r["name"]) for r in shown)
        w_kind = max(len(r["kind"]) for r in shown)
        lines = []
        for r in shown:
            if r["name"] == "user":
                lines.append(f"● {r['name']:<{w_name}}  {r['kind']:<{w_kind}}  —  （用户，网页）")
            elif is_online(r):
                lines.append(f"● {r['name']:<{w_name}}  {r['kind']:<{w_kind}}  {r['cwd']}  在线")
            else:
                lines.append(
                    f"○ {r['name']:<{w_name}}  {r['kind']:<{w_kind}}  {r['cwd']}  离线（{_ago(r['last_seen'])}）"
                )
        return "\n".join(lines)

    # ---- 6.3 send ----

    def send(self, me: int, text: str, to: list[str] | None = None,
             files: list[str] | None = None, *, orig_names: list[str] | None = None,
             text_max: int = TEXT_MAX) -> str:
        """orig_names / text_max 只给 forward 用：保留附件原名、正文多留出「转发自…」那一行。"""
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                me_row = self._me(c, me)
                self._touch(c, me)

                # 1. 解析收件人；有一个名字不存在就整个失败
                to = to or []
                to_ids: list[int] = []
                remote_peers: list[int] = []  # 收件人里别人电脑上的人 → 进发件箱，由网页进程投递
                broadcast = any(t.strip().lower() == "all" or t.strip() == "所有人" for t in to)
                if not broadcast:
                    for t in to:
                        t = t.strip()
                        if not t:
                            continue
                        row = c.execute("SELECT id, name, kind FROM agents WHERE name=?", (t,)).fetchone()
                        if row is None:
                            raise _Fail(
                                f'错误：找不到 "{t}"。现有：{_all_names(c)}'
                            )
                        if row["kind"] == "remote":
                            # 只有用户本人能发给别的电脑上的人；agent 不能把内容直接发出去
                            if me_row["kind"] != "human":
                                raise _Fail(
                                    f"错误：{row['name']} 在另一台电脑上，agent 不能直接发给他。"
                                    "需要的话请用户在网页上转发。"
                                )
                            peer = c.execute("SELECT id FROM peers WHERE agent_id=?", (row["id"],)).fetchone()
                            if peer is None:
                                raise _Fail(f"错误：{row['name']} 已经解除配对，发不出去。")
                            remote_peers.append(int(peer["id"]))
                        if row["id"] not in to_ids:
                            to_ids.append(int(row["id"]))

                # 6. 正文长度（放在写库之前，任何失败都不能有半截数据）
                text = text or ""
                if len(text) > text_max:
                    raise _Fail(
                        f"错误：消息太长（{len(text)} 字 > {text_max}）。长内容请写成文件用 files 发送。"
                    )

                # 2. 附件校验：必须是已存在的文件、单个 ≤20MB、最多 10 个
                resolved: list[Path] = []
                for f in files or []:
                    p = Path(f)
                    if not p.is_absolute():
                        p = Path(me_row["cwd"]) / p
                    p = Path(os.path.abspath(p))
                    if not p.exists():
                        raise _Fail(f"错误：找不到文件：{p}")
                    if not p.is_file():
                        raise _Fail(f"错误：不是文件（可能是文件夹）：{p}")
                    if p.stat().st_size > FILE_MAX:
                        raise _Fail(f"错误：文件超过 20 MB：{p}")
                    resolved.append(p)
                if len(resolved) > FILES_MAX:
                    raise _Fail(f"错误：附件最多 {FILES_MAX} 个。")
                if not text.strip() and not resolved:
                    raise _Fail("错误：消息不能为空。")

                # 3. 写 message 行
                cur = c.execute(
                    "INSERT INTO messages(sender_id, to_ids, text, created_at) VALUES(?, ?, ?, ?)",
                    (me, json.dumps(to_ids), text, now_iso()),
                )
                msg_id = int(cur.lastrowid)

                # 4. 复制附件（3～5 步在同一个事务里，失败回滚并删除已复制的文件）
                files_dir().mkdir(parents=True, exist_ok=True)
                froot = norm(files_dir())
                atts: list[tuple[str, str, int]] = []
                copied: list[Path] = []
                try:
                    for i, p in enumerate(resolved):
                        size = p.stat().st_size
                        np = norm(p)
                        if np == froot or np.startswith(froot + os.sep):
                            dest = p  # 源文件已在 FILES_DIR 里（如 export_context 生成的），不复制
                        else:
                            dest = files_dir() / f"{msg_id:06d}_{i}_{safe(p.name)}"
                            shutil.copy2(p, dest)
                            copied.append(dest)
                        orig = orig_names[i] if orig_names and i < len(orig_names) else p.name
                        atts.append((str(dest), orig, size))
                except OSError as e:
                    for tmp in copied:
                        try:
                            tmp.unlink()
                        except OSError:
                            pass
                    raise _Fail(f"错误：复制附件失败：{e}")

                # 5. 写 attachments 行
                for path_s, orig, size in atts:
                    c.execute(
                        "INSERT INTO attachments(message_id, path, orig_name, size) VALUES(?, ?, ?, ?)",
                        (msg_id, path_s, orig, size),
                    )
                for pid in remote_peers:
                    c.execute("INSERT INTO outbox(message_id, peer_id, status, updated_at) VALUES(?, ?, 'pending', ?)",
                              (msg_id, pid, time.time()))
                names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM agents")}
                c.execute("COMMIT")
            except _Fail as e:
                c.execute("ROLLBACK")
                return str(e)
            except BaseException:
                c.execute("ROLLBACK")
                raise

        disp = "所有人" if (broadcast or not to_ids) else "、".join(names[i] for i in to_ids)
        out = f"已发送 #{msg_id} → {disp}"
        if atts:
            out += f"（附件 {len(atts)} 个）"
        if remote_peers:
            out += "（发往别的电脑的部分会在后台投递）"
        return out

    # ---- 转发（网页用）：把一条消息转给别人，标明原发送者和来源 ----

    def forward(self, me: int, msg_id: int, to: list[str]) -> str:
        with _conn() as c:
            m = c.execute(
                "SELECT m.*, a.name AS sender, a.kind AS sender_kind FROM messages m"
                " JOIN agents a ON a.id = m.sender_id WHERE m.id=?", (msg_id,)
            ).fetchone()
            atts = c.execute("SELECT path, orig_name FROM attachments WHERE message_id=? ORDER BY id",
                             (msg_id,)).fetchall()
        if m is None:
            return f"错误：找不到消息 #{msg_id}"
        if not [t for t in to if t.strip()]:
            return "错误：请选择要转发给谁。"
        where = "另一台电脑" if m["sender_kind"] == "remote" else "本机"
        who = "用户" if m["sender_kind"] == "human" else m["sender"]
        head = f"（转发自 {who} · {where} · {_fmt_time(m['created_at'])} · #{msg_id}）"
        body = m["text"]
        room = TEXT_MAX + FORWARD_EXTRA - len(head) - 1
        if len(body) > room:
            body = body[: room - 20] + "\n…（原文过长，已截断）"
        files = [a["path"] for a in atts if Path(a["path"]).is_file()]
        names = [a["orig_name"] for a in atts if Path(a["path"]).is_file()]
        return self.send(me, head + "\n" + body if body else head, to, files,
                         orig_names=names, text_max=TEXT_MAX + FORWARD_EXTRA)

    # ---- 6.4 read ----

    def read(self, me: int, limit: int = 30) -> str:
        limit = max(1, int(limit))
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                me_row = self._me(c, me)
                self._touch(c, me)
                rows = c.execute(
                    "SELECT * FROM messages WHERE id > ? AND sender_id <> ? ORDER BY id ASC",
                    (me_row["last_read"], me),
                ).fetchall()
                visible = [r for r in rows if self._visible(r["to_ids"], me)]
                take = visible[:limit]
                if take:
                    c.execute("UPDATE agents SET last_read=? WHERE id=?", (take[-1]["id"], me))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        if not take:
            return "没有新消息。"
        remaining = len(visible) - len(take)
        text = self._format(take)
        if remaining:
            text += f"\n（还有 {remaining} 条未读，再调用 read）"
        return text

    # ---- 6.5 history ----

    def history(self, me: int, n: int = 20, with_agent: str | None = None) -> str:
        n = max(1, min(int(n), 100))
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                self._me(c, me)
                self._touch(c, me)
                other_id: int | None = None
                if with_agent is not None:
                    row = c.execute("SELECT id FROM agents WHERE name=?", (with_agent,)).fetchone()
                    if row is None:
                        raise _Fail(f'错误：找不到 "{with_agent}"。现有：{_all_names(c)}')
                    other_id = int(row["id"])
                rows = c.execute("SELECT * FROM messages ORDER BY id ASC").fetchall()
                c.execute("COMMIT")
            except _Fail as e:
                c.execute("ROLLBACK")
                return str(e)
            except BaseException:
                c.execute("ROLLBACK")
                raise
        visible = [m for m in rows if self._between(m, me, other_id)]
        take = visible[-n:]
        if not take:
            return "没有历史消息。"
        return self._format(take)  # 不改变 last_read

    def _between(self, m, me: int, other_id: int | None) -> bool:
        t = self._targets(m["to_ids"])
        broadcast = t == []
        if other_id is None:
            # 我能看到的 = 我发的 + 广播 + 发给我的
            return m["sender_id"] == me or broadcast or me in t
        # 我和对方之间的消息；广播算所有双方
        if m["sender_id"] == other_id:
            return broadcast or me in t
        if m["sender_id"] == me:
            return broadcast or other_id in t
        return False

    # ---- 6.6 rename ----

    def rename(self, me: int, new_name: str) -> str:
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                me_row = self._me(c, me)
                self._touch(c, me)
                try:
                    _check_name(new_name)
                except ValueError as e:
                    raise _Fail(str(e))
                other = c.execute(
                    "SELECT id FROM agents WHERE name=? AND id<>?", (new_name, me)
                ).fetchone()
                if other is not None:
                    raise _Fail(f"错误：名字已被占用：{new_name}")
                old = me_row["name"]
                c.execute("UPDATE agents SET name=? WHERE id=?", (new_name, me))
                c.execute(
                    "INSERT INTO messages(sender_id, to_ids, text, created_at) VALUES(?, '[]', ?, ?)",
                    (me, f"（{old} 改名为 {new_name}）", now_iso()),
                )
                c.execute("COMMIT")
            except _Fail as e:
                c.execute("ROLLBACK")
                return str(e)
            except sqlite3.IntegrityError:
                c.execute("ROLLBACK")
                return f"错误：名字已被占用：{new_name}"
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return f"已改名：{old} → {new_name}"

    # ---- 6.7 set_cwd ----

    def set_cwd(self, me: int, path: str) -> str:
        p = Path(path)
        if not p.is_dir():
            return f"错误：不是已存在的文件夹：{path}"
        stored = str(p.resolve())
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                self._me(c, me)
                self._touch(c, me)
                c.execute("UPDATE agents SET cwd=? WHERE id=?", (stored, me))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return f"已设置工作目录：{stored}"

    # ---- 6.8 export_context（渲染在 export 包里，这里只做包装） ----

    def export_context(self, me: int, mode: str = "clean",
                       session_file: str | None = None) -> str:
        from .export import ExportError, export_context as run_export

        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                me_row = self._me(c, me)
                self._touch(c, me)
                kind, cwd, name = me_row["kind"], me_row["cwd"], me_row["name"]
                thread = me_row["thread_id"]
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        if kind == "codex" and thread and not session_file:
            # Codex 记下了正在用 kopitiam 的对话：直接用它的记录文件，不用按工作目录猜（也就不用先 set_cwd）
            from .export import codex as codex_export
            found = codex_export.find_by_thread(thread)
            if found:
                session_file, cwd = str(found), codex_export.header_cwd(found) or cwd
        try:
            out, stats = run_export(kind, cwd, name, mode=mode, session_file=session_file)
        except ExportError as e:
            return f"错误：{e}"
        return (
            f"已导出 → {out}\n"
            f"（{kind} 记录：{stats['source']}，{stats['turns']} 轮，{_fmt_size(stats['bytes'])}，模式 {mode}）\n"
            "请先打开开头确认是不是当前对话；如果不是，用 session_file 参数指定。\n"
            f"用 send(files=[{out}]) 发给对方。"
        )

    # ---- 网页用的读取接口（不改变任何人的 last_read） ----

    def agents_brief(self) -> list[dict]:
        """所有身份；hidden = 离线超过 7 天（网页主列表不显示，收进「已隐藏」子页面）。"""
        cutoff = _ago_cutoff(WHO_WINDOW)
        with _conn() as c:
            rows = c.execute("SELECT * FROM agents ORDER BY id").fetchall()
            peers = {r["agent_id"]: r for r in c.execute("SELECT * FROM peers")}
        out = []
        for r in rows:
            peer = peers.get(r["id"])
            if r["kind"] == "remote":
                online = peer is not None and peer_online(peer)  # 最近 90 秒内联系上过
            else:
                online = r["kind"] == "human" or is_online(r)
            out.append({
                "id": r["id"],
                "name": r["name"],
                "kind": r["kind"],
                "cwd": r["cwd"],
                "online": online,
                "last_seen": r["last_seen"],
                "hidden": not online and _is_hidden(r, cutoff, set(peers)),
                "peer_name": peer["name"] if peer is not None else None,
            })
        return out

    def hidden_agents(self) -> list[dict]:
        """「已隐藏」子页面：离线超过 7 天的身份，最近出现的排前面，带关联消息数。"""
        cutoff = _ago_cutoff(WHO_WINDOW)
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM agents WHERE kind <> 'human' ORDER BY last_seen DESC"
            ).fetchall()
            counts = _message_counts(c)
            paired = _peer_agent_ids(c)
        return [
            {
                "name": r["name"],
                "kind": r["kind"],
                "cwd": r["cwd"],
                "last_seen": r["last_seen"],
                "ago": _ago(r["last_seen"]),
                "messages": counts.get(r["id"], 0),
            }
            for r in rows if _is_hidden(r, cutoff, paired)
        ]

    def forget(self, name: str) -> str:
        """网页手动删除一个身份：只允许离线、而且没有任何消息关联的（否则历史消息会丢名字）。"""
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
                if row is None:
                    raise _Fail(f'错误：找不到 "{name}"')
                if row["kind"] == "human":
                    raise _Fail("错误：不能删除用户身份")
                if is_online(row):
                    raise _Fail(f"错误：{row['name']} 还在线，不能删除")
                if c.execute("SELECT 1 FROM peers WHERE agent_id=?", (row["id"],)).fetchone():
                    raise _Fail(f"错误：{row['name']} 还在团队里，请先解除配对")
                n = _message_counts(c).get(row["id"], 0)
                if n:
                    raise _Fail(f"错误：{row['name']} 有 {n} 条相关消息，只能隐藏、不能删除")
                c.execute("DELETE FROM agents WHERE id=?", (row["id"],))
                c.execute("COMMIT")
            except _Fail as e:
                c.execute("ROLLBACK")
                return str(e)
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return f"已删除 {row['name']}"

    def messages_after(self, after: int, limit: int = 200) -> list[dict]:
        """user 能看到所有消息（包括 agent 之间互发的）。"""
        # 首次加载（after=0）取最新的 limit 条；之后按升序取，超过 limit 的下次轮询再补，不会漏
        order = "DESC" if after <= 0 else "ASC"
        with _conn() as c:
            rows = c.execute(
                f"SELECT * FROM messages WHERE id > ? ORDER BY id {order} LIMIT ?",
                (after, limit),
            ).fetchall()
            if order == "DESC":
                rows.reverse()
            return self._to_json(c, rows)

    def messages_with(self, name: str, limit: int = 200) -> list[dict]:
        """某个 agent 发的、和单独发给它的最近 limit 条（id 升序）。网页进入对话时用来补全历史。"""
        with _conn() as c:
            row = c.execute("SELECT id FROM agents WHERE name=?", (name,)).fetchone()
            if row is None:
                return []
            aid = row["id"]
            rows = []
            for m in c.execute("SELECT * FROM messages ORDER BY id DESC"):
                if m["sender_id"] == aid or aid in self._targets(m["to_ids"]):
                    rows.append(m)
                    if len(rows) >= limit:
                        break
            rows.reverse()
            return self._to_json(c, rows)

    def _to_json(self, c, rows) -> list[dict]:
        names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM agents")}
        kinds = {r["id"]: r["kind"] for r in c.execute("SELECT id, kind FROM agents")}
        atts: dict[int, list[sqlite3.Row]] = {}
        ids = [m["id"] for m in rows]
        delivery: dict[int, dict] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for r in c.execute(f"SELECT * FROM attachments WHERE message_id IN ({marks}) ORDER BY id", ids):
                atts.setdefault(r["message_id"], []).append(r)
            delivery = _delivery(c, f"message_id IN ({marks})", ids)
        out = []
        for m in rows:
            item = {
                "id": m["id"],
                "time": m["created_at"],
                "sender": names.get(m["sender_id"], "?"),
                "sender_kind": kinds.get(m["sender_id"], "?"),
                "to": [names.get(i, "?") for i in self._targets(m["to_ids"])],
                "text": m["text"],
                "files": [
                    {"name": a["orig_name"], "path": a["path"], "size": a["size"]}
                    for a in atts.get(m["id"], [])
                ],
            }
            if m["id"] in delivery:
                item["delivery"] = delivery[m["id"]]
            out.append(item)
        return out

    def delivery_updates(self) -> dict[str, dict]:
        """发往别的电脑的消息里，还没送达的、和最近 10 分钟内状态变过的；网页用来刷新「已送达」。"""
        with _conn() as c:
            return {str(k): v for k, v in _delivery(
                c, "message_id IN (SELECT message_id FROM outbox WHERE status='pending' OR updated_at > ?)",
                [time.time() - 600]).items()}

    # ---- 格式化 ----

    def _format(self, rows) -> str:
        with _conn() as c:
            names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM agents")}
            atts: dict[int, list[sqlite3.Row]] = {}
            for r in c.execute("SELECT * FROM attachments ORDER BY id"):
                atts.setdefault(r["message_id"], []).append(r)
        blocks = []
        for m in rows:
            sender = names.get(m["sender_id"], "?")
            targets = self._targets(m["to_ids"])
            to_disp = "所有人" if not targets else "、".join(names.get(i, "?") for i in targets)
            lines = [f"#{m['id']} {_fmt_time(m['created_at'])} {sender} → {to_disp}", m["text"]]
            for a in atts.get(m["id"], []):
                lines.append(f"  📎 {a['path']} ({_fmt_size(a['size'])})")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
