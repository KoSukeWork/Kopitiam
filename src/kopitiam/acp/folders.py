"""新建托管会话时选工作目录：弹出本机的「选择文件夹」窗口，以及列出其他 agent 用过的项目文件夹。

浏览器出于安全考虑拿不到文件夹的完整路径，所以由 kopitiam web（就在用户的电脑上运行）弹出 Windows 的窗口。
窗口放在单独的进程里：tkinter 只能在一个线程里用，而且窗口出任何问题都不会影响网页服务。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from contextlib import closing

from .. import db
from ..config import norm

PICK_TIMEOUT = 600  # 窗口开着没人管时，最多等这么久（秒）
KNOWN_MAX = 20

# Tk 8.6 在 Windows 上用的是系统自带的新式「选择文件夹」窗口（IFileDialog）
PICK_SCRIPT = r"""
import json, os, sys, tkinter
from tkinter import filedialog
initial = sys.argv[1] if len(sys.argv) > 1 else ""
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)  # 尽量弹在浏览器前面
root.lift()
path = filedialog.askdirectory(parent=root, title="选择 agy 会话的工作目录", mustexist=True,
                               initialdir=initial if os.path.isdir(initial) else None)
root.destroy()
sys.stdout.write(json.dumps({"path": path or ""}))
"""

_picking = threading.Lock()


def run_picker(initial: str) -> str:
    """弹出窗口，等用户选完；返回选中的文件夹，取消就是空字符串。"""
    result = subprocess.run([sys.executable, "-I", "-c", PICK_SCRIPT, initial], capture_output=True,
                            timeout=PICK_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        return str(json.loads(result.stdout or b"{}").get("path") or "")
    except ValueError:
        raise ValueError("选择文件夹的窗口没能打开，请直接填写路径") from None


def pick(initial: str = "") -> str:
    if not isinstance(initial, str):
        raise ValueError("initial 必须是文字")
    if not _picking.acquire(blocking=False):
        raise ValueError("已经开着一个选择文件夹的窗口，请先在那个窗口里选择或取消")
    try:
        path = run_picker(initial.strip())
    except subprocess.TimeoutExpired:
        raise ValueError("选择文件夹的窗口等太久没有选择，已关闭") from None
    finally:
        _picking.release()
    return os.path.normpath(path) if path else ""


def known() -> list[str]:
    """其他 agent 用过的、现在还在的项目文件夹，最近用过的在前。"""
    with closing(db.connect()) as c:
        rows = c.execute("SELECT cwd, launch_cwd FROM agents WHERE kind NOT IN ('human', 'remote')"
                         " AND trashed_at IS NULL ORDER BY last_seen DESC").fetchall()
    seen, out = set(), []
    for row in rows:
        for path in (row["cwd"], row["launch_cwd"]):
            if not path or norm(path) in seen:
                continue
            seen.add(norm(path))
            # 家目录、根目录这类不是项目，不列
            if os.path.isdir(path) and norm(path) != norm(os.path.expanduser("~")) and os.path.dirname(norm(path)) != norm(path):
                out.append(path)
        if len(out) >= KNOWN_MAX:
            break
    return out[:KNOWN_MAX]
