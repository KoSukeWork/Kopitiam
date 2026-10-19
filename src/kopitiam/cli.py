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
    pw.add_argument("--no-wake", action="store_true", help="这次不运行自动唤醒")

    sub.add_parser("wake", help="单独运行自动唤醒（不开网页时用；开着 kopitiam web 就不需要）")

    ps = sub.add_parser("setup", help="给 agent 安装自动唤醒需要的配置")
    ps.add_argument("agent", choices=["claude"])
    ps.add_argument("--print", dest="print_only", action="store_true", help="只打印要加的配置，不改文件")
    ps.add_argument("--remove", action="store_true", help="删掉 kopitiam 加的配置")

    ph = sub.add_parser("hook", help="给 agent 的 hook 调用（不用手动运行）")
    ph.add_argument("event", choices=["claude-start", "claude-wake"])

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
            peer_host=args.peer_host, peer_port=args.peer_port, share=args.share, wake=not args.no_wake)
    if args.cmd == "export":
        return _run_export(args)
    if args.cmd == "wake":
        return _run_wake()
    if args.cmd == "setup":
        return _run_setup(args)
    if args.cmd == "hook":
        return _run_hook(args.event)
    return 0


def _run_wake() -> int:
    from .wake.service import WakeService

    def show(call, err) -> None:
        print(f"{'叫醒失败' if err else '已叫醒'} {call.name}：{err or call.text}", flush=True)

    print("自动唤醒已启动（Ctrl+C 退出）", flush=True)
    try:
        WakeService(on_event=show).run_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _run_setup(args) -> int:
    import json

    from .wake import claude

    try:
        exe = claude.exe_path()
        if args.print_only:
            print(json.dumps({"hooks": {k: [v] for k, v in claude.hook_entries(exe).items()}},
                             ensure_ascii=False, indent=2))
            return 0
        path = claude.settings_path()
        done = claude.install(path, exe, remove=args.remove)
    except (RuntimeError, OSError, ValueError) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    print(f"{done}：{path}（原文件已备份成 {path.name}.bak）")
    if not args.remove:
        print("重开 Claude Code 后生效。")
    return 0


def _run_hook(event: str) -> int:
    """hook 入口：stdin 是 Claude Code 给的 JSON。任何意外都静默退出 0，不能打扰 Claude。"""
    from .wake import claude

    try:
        stdin = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        if event == "claude-start":
            out = claude.hook_start()
            if out:
                sys.stdout.write(out + "\n")
            return 0
        code, text = claude.hook_wake(stdin)
        if text:
            sys.stderr.buffer.write((text + "\n").encode("utf-8"))  # Windows 管道默认不是 UTF-8
        return code
    except Exception:
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
