"""dsh 适配器：靠 dsh 的 cordis 插件叫醒（sessionController.prompt，和在网页上发消息一样），闲着也能叫醒。

- 插件 dsh_wake.mjs 由 kopitiam setup dsh 复制到 ~/.dsh/profiles/<profile>/kopitiam-wake.mjs，
  并在那个 profile 的 cordis.patch.yml 末尾加一段 insert（用标记行包起来，--remove 时整段删掉）。
  web profile 默认 patchReload: live，改完不用重开 dsh。
- 一个 dsh 进程管着好几个会话：插件自己记下最近调用 kopitiam 工具的会话，叫醒时发给它。
- 插件知道该发给哪个会话以后才建信号文件 wake/dsh-<dsh 进程号>.signal（kopitiam 的 MCP 进程也是 dsh 进程启动的，
  进程号对得上）。所以信号文件在 = 能叫醒。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import signals

PLUGIN = Path(__file__).with_name("dsh_wake.mjs")
INSTALLED_NAME = "kopitiam-wake.mjs"
BEGIN = "# >>> kopitiam 自动唤醒（kopitiam setup dsh 加的；kopitiam setup dsh --remove 会删掉这一段）"
END = "# <<< kopitiam 自动唤醒"


def check(agent: dict) -> str | None:
    if signals.ready("dsh", agent):
        return None
    return ("dsh 还叫不醒：没装唤醒插件（运行 kopitiam setup dsh），"
            "或者还没在会话里用过 kopitiam（先让要接收消息的那个会话调用一次 whoami）")


def wake(call) -> None:
    signals.write("dsh", call)


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


def install(profile: Path, *, remove: bool = False) -> Path:
    """装进（或删掉）一个 profile；返回插件文件的位置。改之前先备份成 cordis.patch.yml.bak-kopitiam。"""
    patch = profile / "cordis.patch.yml"
    text = ""
    if patch.exists():
        text = patch.read_text(encoding="utf-8")
        shutil.copy2(patch, patch.with_name(patch.name + ".bak-kopitiam"))
    text = _strip(text)
    plugin = profile / INSTALLED_NAME
    if remove:
        plugin.unlink(missing_ok=True)
    else:
        shutil.copyfile(PLUGIN, plugin)  # 先放插件再改 patch：web profile 一改 patch 就会热加载
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + patch_block(plugin)
    patch.write_text(text, encoding="utf-8")
    return plugin
