"""pi 适配器：靠 pi 的扩展叫醒（pi.sendUserMessage），不需要别的配置。

- 扩展 pi_wake.ts 由 kopitiam setup pi 复制到 ~/.pi/agent/extensions/kopitiam-wake.ts（pi 用 jiti 直接加载 .ts）。
- pi 启动时扩展建信号文件 wake/pi-<pi 进程号>.signal 并监视它；扩展和 kopitiam MCP 都在这个 pi 进程下面，
  所以 process.pid 就是 MCP 认领身份时找到的 agent 进程。
- 要叫醒时 wake() 写信号文件，扩展读到后 sendUserMessage（pi 正忙就排在这一轮后面）。
- 用户自己在 pi 里打了字，扩展写 wake/pi-<pi 进程号>.typed（{"at": 时间}）；typed() 拿走它，
  这个 pi 和别的 agent 之间的来回重新计数（见 detector.user_spoke）。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import detector, signals

EXTENSION = Path(__file__).with_name("pi_wake.ts")
INSTALLED_NAME = "kopitiam-wake.ts"


def check(agent: dict) -> str | None:
    if signals.ready("pi", agent):
        return None
    return "pi 没装唤醒扩展（运行 kopitiam setup pi，然后重开 pi）"


def wake(call) -> None:
    signals.write("pi", call)


def typed() -> None:
    for f, obj in signals.take_typed("pi-*.typed"):
        pid = f.stem.rpartition("-")[2]
        if pid.isdigit():
            detector.user_spoke("pi", pid=int(pid), at=obj.get("at"))


def extensions_dir() -> Path:
    base = os.environ.get("PI_CODING_AGENT_DIR")
    return (Path(base) if base else Path.home() / ".pi" / "agent") / "extensions"


def install(dest_dir: Path, *, remove: bool = False) -> Path:
    """把扩展复制进 pi 的扩展目录（或删掉）；只动 kopitiam-wake.ts 这一个文件。"""
    dest = dest_dir / INSTALLED_NAME
    if remove:
        dest.unlink(missing_ok=True)
    else:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXTENSION, dest)
    return dest
