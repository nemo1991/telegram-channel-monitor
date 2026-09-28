"""CLI 入口:argparse 子命令(sync / monitor)。

不动 GUI:被 `tgmonitor.__main__` 在识别到子命令时调用,任何分支都不
import PySide6 / qasync / `tgmonitor.ui.*`。
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def _ensure_utf8_stdio() -> None:
    """2026-09-28:Windows 默认 cp1252 编码不了 argparse help 里的中文,
    `parser.print_help()` / `parser.error()` 路径直接抛
    `UnicodeEncodeError`。这里在 CLI 入口先把 stdout / stderr reconfigure
    成 UTF-8(`errors="replace"` 兜底,避免极端字符)。Mac / Linux 上 stdout
    默认 UTF-8,reconfigure 是 no-op。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                # 某些嵌入式 / 重定向场景 reconfigure 失败 — 不阻塞 CLI 启动
                pass


def build_parser() -> argparse.ArgumentParser:
    """构造 argparse:顶层 + sync / monitor 子命令。"""
    parser = argparse.ArgumentParser(
        prog="tgmonitor",
        description="Telegram 频道监听(CLI 模式 — 无 GUI)",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<cmd>")

    # ---- sync ----
    sync_p = sub.add_parser(
        "sync",
        help="全量同步指定频道(元数据 + 历史消息),完成后退出",
    )
    sync_p.add_argument(
        "channel_ids",
        type=int,
        nargs="+",
        help="要同步的 Telegram chat_id(空格分隔多个)",
    )
    sync_p.add_argument(
        "--no-metadata",
        action="store_true",
        help="跳过元数据刷新",
    )
    sync_p.add_argument(
        "--no-history",
        action="store_true",
        help="跳过历史消息",
    )
    sync_p.add_argument(
        "--resume",
        action="store_true",
        help="从 storage 最大 msg_id 之后续拉(避免重复)",
    )
    sync_p.add_argument(
        "--media-policy",
        choices=["metadata", "thumbnail", "full"],
        default="metadata",
        help="媒体下载策略(默认仅元数据 — CLI sync 适合走 metadata 节省带宽)",
    )
    sync_p.add_argument(
        "--chat-delay-ms",
        type=int,
        default=500,
        help="频道间 / 单条间隔 ms(防封号,默认 500)",
    )
    sync_p.add_argument(
        "--page-delay-ms",
        type=int,
        default=1000,
        help="每 100 条消息的翻页间隔 ms(默认 1000)",
    )

    # ---- monitor ----
    mon_p = sub.add_parser(
        "monitor",
        help="前台启动监听服务(SIGINT / SIGTERM 退出)",
    )
    mon_p.add_argument(
        "--no-resume",
        action="store_true",
        help="忽略 .env 中 TG_PAUSED=true,强制启动 monitor",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口:解析 argv,路由到 sync / monitor 实现。

    返回退出码:0 成功 / 1 错误 / 130 SIGINT(沿用 `__main__` 既有约定)。
    """
    # Windows 中文 locale 下 argparse 打印会因 cp1252 编码炸,先 reconfigure
    _ensure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "sync":
        from tgmonitor.cli.sync import run_sync

        return asyncio.run(run_sync(args)) or 0
    if args.cmd == "monitor":
        from tgmonitor.cli.monitor import run_monitor

        return asyncio.run(run_monitor(args)) or 0
    # 没指定子命令 → 打印帮助
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
