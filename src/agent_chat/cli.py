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
        from .web import run
        run(args.port, open_browser=not args.no_open)
    if args.cmd == "export":
        return _run_export(args)
    return 0


def _run_export(args) -> int:
    from .export import ExportError, export_context

    try:
        out, stats = export_context(
            args.agent, args.cwd, args.agent,
            mode=args.mode, session_file=args.session_file,
        )
    except ExportError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    print(f"已导出 → {out}")
    print(f"（来源 {stats['source']}，{stats['turns']} 轮，{stats['bytes']} 字节，模式 {args.mode}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
