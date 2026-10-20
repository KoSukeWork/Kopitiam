"""dsh 适配器：靠 dsh 的 cordis 插件叫醒（sessionController.prompt，和在网页上发消息一样），闲着也能叫醒。

- 插件 dsh_wake.mjs 由 kopitiam setup dsh 复制到 ~/.dsh/profiles/<profile>/kopitiam-wake-<哈希>.mjs，
  并在那个 profile 的 cordis.patch.yml 末尾加一段 insert（用标记行包起来，--remove 时整段删掉）。
  web profile 默认 patchReload: live，改完不用重开 dsh。
- 一个 dsh 进程管着好几个会话，只有一个 kopitiam MCP 进程，调用工具时 dsh 也不带会话信息：
  插件在每次调用 kopitiam 的工具之前，把是哪个会话写进 wake/dsh-<dsh 进程号>.calling.json（calling 读它），
  于是一个会话一个身份（会话 ID 记在 agents.thread_id）。
- 叫醒某个会话：写 wake/dsh-<dsh 进程号>.wake-<叫醒编号>.json，插件读到就发进那个会话、删掉文件。
- 插件一加载就建 wake/dsh-<dsh 进程号>.signal（kopitiam 的 MCP 进程也是 dsh 进程启动的，进程号对得上），
  所以它在 = 装好了插件。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from ..config import safe, wake_dir
from . import signals

PLUGIN = Path(__file__).with_name("dsh_wake.mjs")
BEGIN = "# >>> kopitiam 自动唤醒（kopitiam setup dsh 加的；kopitiam setup dsh --remove 会删掉这一段）"
END = "# <<< kopitiam 自动唤醒"


def calling(host: int) -> tuple[str, str | None, str | None] | None:
    """插件写下的「现在是哪个会话在调用 kopitiam」：(会话 ID, 工作目录, 标题)；没装新插件就是 None。"""
    try:
        obj = json.loads((wake_dir() / f"dsh-{host}.calling.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sid = obj.get("session") if isinstance(obj, dict) else None
    if not sid:
        return None
    return str(sid), str(obj.get("cwd") or "") or None, str(obj.get("title") or "") or None


def check(agent: dict) -> str | None:
    if not signals.ready("dsh", agent):
        return "dsh 还叫不醒：没装唤醒插件（运行 kopitiam setup dsh）"
    if not agent.get("thread_id"):
        return "还不知道该发给哪个会话：先让要接收消息的那个会话调用一次 kopitiam（比如 whoami）"
    return None


def wake(call) -> None:
    if not signals.path("dsh", call.pid).exists():
        raise RuntimeError("信号文件不见了（dsh 可能已经关掉）")
    target = wake_dir() / f"dsh-{call.pid}.wake-{safe(call.wake_id)}.json"
    tmp = target.with_name(f".{target.name}.tmp")  # 写完再改名：插件只认最终的名字，不会读到半截
    tmp.write_text(json.dumps({"id": call.wake_id, "text": call.text, "session": call.thread_id},
                              ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


# ---- 安装：复制插件，在 cordis.patch.yml 末尾加一段 insert ----

def profiles_dir() -> Path:
    return Path.home() / ".dsh" / "profiles"


def target_profiles(base: Path) -> list[Path]:
    """默认装进已经配了 kopitiam MCP 的 profile（cordis.patch.yml 里有 serverName: kopitiam）。"""
    return [p.parent for p in sorted(base.glob("*/cordis.patch.yml"))
            if "serverName: kopitiam" in p.read_text(encoding="utf-8")]


def patch_block(plugin: Path) -> str:
    path = str(plugin).replace("'", "''")  # YAML 单引号字符串：反斜杠原样保留，单引号要写两遍
    return f"{BEGIN}\n- insert:\n    - id: kopitiam-wake\n      name: '{path}'\n{END}\n"


def _strip(text: str) -> str:
    """去掉以前加的那一段（标记行之间，含标记行）。"""
    out, inside = [], False
    for line in text.splitlines(keepends=True):
        if line.rstrip("\r\n") == BEGIN:
            inside = True
        elif inside and line.rstrip("\r\n") == END:
            inside = False
        elif not inside:
            out.append(line)
    return "".join(out)


def installed_name() -> str:
    """装进 profile 的插件文件名带上内容的哈希。dsh 用 import() 加载插件，同一个路径拿到的是缓存里的旧模块；
    插件一改路径就变，web profile 热加载时才会真的换成新插件，不用重开 dsh。"""
    return f"kopitiam-wake-{hashlib.sha256(PLUGIN.read_bytes()).hexdigest()[:8]}.mjs"


def install(profile: Path, *, remove: bool = False) -> Path:
    """装进（或删掉）一个 profile；返回插件文件的位置。改之前先备份成 cordis.patch.yml.bak-kopitiam。"""
    patch = profile / "cordis.patch.yml"
    text = ""
    if patch.exists():
        text = patch.read_text(encoding="utf-8")
        shutil.copy2(patch, patch.with_name(patch.name + ".bak-kopitiam"))
    text = _strip(text)
    plugin = profile / installed_name()
    if not remove:
        shutil.copyfile(PLUGIN, plugin)  # 先放插件再改 patch：web profile 一改 patch 就会热加载
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + patch_block(plugin)
    patch.write_text(text, encoding="utf-8")
    for old in profile.glob("kopitiam-wake*.mjs"):  # 以前装的版本（dsh 已经加载的留在内存里，删文件不影响）
        if remove or old != plugin:
            old.unlink(missing_ok=True)
    return plugin
