"""SQLite 存储层：建表、连接、身份认领、Chat 业务逻辑。

业务逻辑都放在 Chat 里（第一个业务参数是 me: int，即 agent id），MCP 工具和网页都是薄包装。
所有时间戳用本地时间 ISO 字符串（精确到秒）；消息里只存 sender_id，显示时再取当前名字。
写操作都放在 BEGIN IMMEDIATE ... COMMIT 里，方法失败返回以「错误：」开头的文本，不抛异常
（身份认领 claim 除外——它在进程启动时调用，失败要直接退出）。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import psutil

from .config import (
    FILE_MAX,
    FILES_MAX,
    TEXT_MAX,
    WHO_WINDOW,
    data_dir,
    db_path,
    files_dir,
    norm,
    safe,
    valid_name,
)

SCHEMA_VERSION = "1"

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
"""


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
            if c.execute("SELECT 1 FROM meta WHERE key='schema_version'").fetchone() is None:
                c.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (SCHEMA_VERSION,))
            c.execute(
                "INSERT OR IGNORE INTO agents(name, kind, cwd, launch_cwd, pid, pid_ctime,"
                " last_read, created_at, last_seen) VALUES('user', 'human', ?, '', NULL, NULL, 0, ?, ?)",
                (str(Path.home()), now_iso(), now_iso()),
            )
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")


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


def claim(kind: str, cwd: str | None = None, *, name: str | None = None,
          pid: int | None = None, pid_ctime: float | None = None) -> int:
    """认领身份，返回 agent id。cwd 默认当前进程目录（MCP 启动时的 os.getcwd()）。"""
    if cwd is None:
        cwd = os.getcwd()
    cwd = str(cwd)
    if pid is None:
        pid = os.getpid()
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
                if row is not None and not is_online(row):
                    # 认领这一行：写 pid、pid_ctime、last_seen，kind 也更新
                    c.execute(
                        "UPDATE agents SET pid=?, pid_ctime=?, last_seen=?, kind=? WHERE id=?",
                        (pid, pid_ctime, now_iso(), kind, row["id"]),
                    )
                    c.execute("COMMIT")
                    return int(row["id"])
                if row is not None:
                    # 名字被在线的进程占用：改用 名字-2、名字-3……第一个空闲的，新建一行
                    i = 2
                    while _name_taken(c, f"{name}-{i}"):
                        i += 1
                    name = f"{name}-{i}"
                agent_id = _insert_agent(c, name=name, kind=kind, cwd=cwd,
                                         launch_cwd=cwd, pid=pid, pid_ctime=pid_ctime)
                c.execute("COMMIT")
                return agent_id

            # 没给 name：找 kind 相同、launch_cwd 相同、不在线的行，取 last_seen 最新的认领
            candidates = c.execute(
                "SELECT * FROM agents WHERE kind=? ORDER BY last_seen DESC", (kind,)
            ).fetchall()
            for cand in candidates:
                if norm(cand["launch_cwd"]) == norm(cwd) and not is_online(cand):
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


def release(agent_id: int) -> None:
    """进程退出时把自己这一行的 pid 设为 NULL（atexit 调；崩溃的进程靠 is_online 判断）。"""
    try:
        with _conn() as c:
            c.execute("UPDATE agents SET pid=NULL, pid_ctime=NULL WHERE id=?", (agent_id,))
    except Exception:
        pass  # 退出清理尽力而为


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
        cutoff = (datetime.now() - timedelta(seconds=WHO_WINDOW)).isoformat(timespec="seconds")
        shown = [r for r in rows if r["name"] == "user" or r["last_seen"] >= cutoff]
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
             files: list[str] | None = None) -> str:
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                me_row = self._me(c, me)
                self._touch(c, me)

                # 1. 解析收件人；有一个名字不存在就整个失败
                to = to or []
                to_ids: list[int] = []
                broadcast = any(t.strip().lower() == "all" or t.strip() == "所有人" for t in to)
                if not broadcast:
                    for t in to:
                        t = t.strip()
                        if not t:
                            continue
                        row = c.execute("SELECT id FROM agents WHERE name=?", (t,)).fetchone()
                        if row is None:
                            raise _Fail(
                                f'错误：找不到 "{t}"。现有：{_all_names(c)}'
                            )
                        if row["id"] not in to_ids:
                            to_ids.append(int(row["id"]))

                # 6. 正文长度（放在写库之前，任何失败都不能有半截数据）
                text = text or ""
                if len(text) > TEXT_MAX:
                    raise _Fail(
                        f"错误：消息太长（{len(text)} 字 > {TEXT_MAX}）。长内容请写成文件用 files 发送。"
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
                        atts.append((str(dest), p.name, size))
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
        return out

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
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
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
        with _conn() as c:
            rows = c.execute("SELECT * FROM agents ORDER BY id").fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "kind": r["kind"],
                "cwd": r["cwd"],
                "online": r["name"] == "user" or is_online(r),
            }
            for r in rows
        ]

    def messages_after(self, after: int, limit: int = 200) -> list[dict]:
        """user 能看到所有消息（包括 agent 之间互发的）。"""
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE id > ? ORDER BY id DESC LIMIT ?",
                (after, limit),
            ).fetchall()
            names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM agents")}
            kinds = {r["id"]: r["kind"] for r in c.execute("SELECT id, kind FROM agents")}
            atts: dict[int, list[sqlite3.Row]] = {}
            for r in c.execute("SELECT * FROM attachments ORDER BY id"):
                atts.setdefault(r["message_id"], []).append(r)
        out = []
        for m in reversed(rows):  # 恢复成 id 升序
            out.append({
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
            })
        return out

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
