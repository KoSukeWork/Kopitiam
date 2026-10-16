"""命令行入口：agent-chat mcp / web / export。"""
from __future__ import annotations

import argparse
import sys

KINDS = ["claude", "codex", "pi", "dsh", "agent"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-chat", description="本地聊天室：多个 coding agent 和用户互发消息、文件"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("mcp", help="启动 stdio MCP server（给 agent 用）")
    pm.add_argument("--kind", required=True, choices=KINDS, help="自己是哪种 agent")
    pm.add_argument("--name", default=None, help="固定名字（可选）")

    pw = sub.add_parser("web", help="启动本地网页")
    pw.add_argument("--port", type=int, default=8765)
    pw.add_argument("--no-open", action="store_true", help="不自动打开浏览器")

    pe = sub.add_parser("export", help="调试用：导出某个 agent 的对话记录")
    pe.add_argument("--agent", required=True, choices=["claude", "codex", "pi", "dsh"])
    pe.add_argument("--cwd", required=True)
    pe.add_argument("--mode", default="clean", choices=["clean", "full"])
    pe.add_argument("--session-file", default=None)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "mcp":
        from .mcp_server import run
        run(args.kind, args.name)
    if args.cmd == "web":
        raise SystemExit("尚未实现：web（里程碑 6）")
    if args.cmd == "export":
        raise SystemExit("尚未实现：export（里程碑 4）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
