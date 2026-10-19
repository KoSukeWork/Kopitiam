"""往 agent 的 hooks 配置（Claude 的 settings.json、Codex 的 hooks.json）里加 / 删 kopitiam 的 hook。

两边格式一样：{"hooks": {事件名: [{"matcher"?, "hooks": [{"type": "command", …}]}]}}。
只增删 kopitiam 自己的那几组，别的内容原样保留；改之前先备份成 <文件名>.bak。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Callable


def exe_path() -> str:
    """kopitiam 可执行文件的绝对路径（hook 里要写绝对路径）。"""
    me = Path(sys.argv[0])
    if me.stem.lower() == "kopitiam" and me.exists():
        return str(me.resolve())
    found = shutil.which("kopitiam")
    if found:
        return str(Path(found).resolve())
    raise RuntimeError("找不到 kopitiam 可执行文件，请先用 uv tool install 安装")


def merge(path: Path, entries: dict, is_ours: Callable[[dict], bool], *, remove: bool = False) -> str:
    data: dict = {}
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    hooks = data.setdefault("hooks", {})
    for event, group in entries.items():
        groups = [g for g in hooks.get(event, []) if not is_ours(g)]
        if not remove:
            groups.append(group)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return "已删除" if remove else "已安装"
