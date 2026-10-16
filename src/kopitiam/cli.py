"""命令行入口：kopitiam mcp / web / export。"""
from __future__ import annotations

import argparse
import sys

KINDS = ["claude", "codex", "pi", "dsh", "agent"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kopitiam", description="本地聊天室：多个 coding agent 和用户互发消息、文件"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("mcp", help="启动 stdio MCP server（给 agent 用）")
    pm.add_argument("--kind", required=True, choices=KINDS, help="自己是哪种 agent")
    pm.add_argument("--name", default=None, help="固定名字（可选）")

    pw = sub.add_parser("web", help="启动本地网页")
    pw.add_argument("--port", type=int, default=8765)
    pw.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    share = pw.add_mutually_exclusive_group()
    share.add_argument("--share", dest="share", action="store_const", const=True, default=None,
                       help="这次启动就打开团队连接（默认按上次在网页上的设置）")
    share.add_argument("--no-share", dest="share", action="store_const", const=False,
                       help="这次不打开团队连接")
    pw.add_argument("--peer-port", type=int, default=8766, help="团队端口（默认 8766）")
    pw.add_argument("--peer-host", default="0.0.0.0",
                    help="团队端口绑定的地址（默认所有网卡；只想走 Tailscale 就填 Tailscale 的 IP）")

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
        run(args.port, open_browser=not args.no_open,
            peer_host=args.peer_host, peer_port=args.peer_port, share=args.share)
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
