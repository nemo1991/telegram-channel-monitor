"""CLI 入口:argparse 子命令(2.1.0 完备命令体系,2.1.2 唯一 CLI 命令)。

不动 GUI:console script `tgmon-cli` 直连本模块 `main_tgmon_cli`,从进程起点
就不 import `tgmonitor.app`,任何分支都加载不到 PySide6 / qasync。GUI 命令
`tgmon` 完全不碰 CLI(见 `tgmonitor.__main__`)。

`CLI_SUBCOMMANDS` + `_DISPATCH` 是命令注册表单一真源,新增子命令需在
`build_parser()` 注册 + 两个集合同步。

`help` 子命令打印帮助总览;未知参数与组命令缺动作(channels/messages/
media/export/config 未跟子动作)均由 argparse 报用法错误(退出码 2)。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from tgmonitor.cli._common import CliError

# 2026-11-25 P1-E:export choices 由 Exporter 注册表驱动(单一真源)。
# service 是纯 core 模块(已验证不 import Qt/qasync),不破坏 CLI"零 GUI 依赖"。
from tgmonitor.core.export import service as _export_service  # noqa: F401
from tgmonitor.core.export.base import EXPORTERS

# 命令注册表单一真源(新增子命令需与 _DISPATCH 同步)
CLI_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "sync",
        "monitor",
        "status",
        "login",
        "logout",
        "channels",
        "pause",
        "resume",
        "messages",
        "media",
        "export",
        "config",
        "help",
    }
)


def _ensure_utf8_stdio() -> None:
    """Windows 默认 cp1252 编码不了 argparse help 里的中文,入口先 reconfigure
    成 UTF-8(`errors="replace"` 兜底)。Mac / Linux 上是 no-op。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    """给查询/状态类子命令统一加 `--json`。"""
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出到 stdout(便于脚本消费)",
    )


_MEDIA_TYPES = [
    "photo",
    "video",
    "audio",
    "voice",
    "document",
    "sticker",
    "animation",
    "video_note",
]


def build_parser(prog: str = "tgmon-cli") -> argparse.ArgumentParser:
    """构造 argparse:顶层 + 全部子命令。

    `prog` 默认 `tgmon-cli`(唯一 CLI 命令);测试可传别名以核对 usage。
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Telegram 频道监听(CLI 模式 — 无 GUI)",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<cmd>")

    # ---- status ----
    status_p = sub.add_parser("status", help="查看登录/监听状态与统计")
    _add_json_flag(status_p)
    status_p.add_argument(
        "--channels",
        action="store_true",
        help="展开逐频道统计明细",
    )

    # ---- login ----
    login_p = sub.add_parser("login", help="headless 二维码登录")
    login_p.add_argument("--api-id", type=int, help="Telegram API ID(写 .env)")
    login_p.add_argument("--api-hash", help="Telegram API Hash(写 .env)")
    login_p.add_argument(
        "--show-url",
        action="store_true",
        help="把含 token 的二维码 URL 也打印到 stderr(终端可信时用)",
    )

    # ---- logout ----
    logout_p = sub.add_parser("logout", help="登出(清除本地 session)")
    logout_p.add_argument("--yes", action="store_true", help="确认执行")

    # ---- channels ----
    ch_p = sub.add_parser("channels", help="监听频道管理")
    # 组内子动作 required=True(2.1.6 P2 #15):缺动作在解析期就报用法错
    # (退出码 2,stderr),不进 dispatch / bootstrap。
    ch_sub = ch_p.add_subparsers(dest="channel_cmd", required=True)
    ch_list = ch_sub.add_parser("list", help="列出频道")
    ch_list.add_argument(
        "--source",
        choices=["joined", "monitored", "subscribed"],
        default="monitored",
        help="joined=连 TG 拉取 / monitored=白名单(默认) / subscribed=TG 已订阅",
    )
    _add_json_flag(ch_list)
    # refresh 会级联停掉"已在 TG 退出频道"的监听(写操作),与 add/remove 一致
    # 强制 --yes(2.1.6 P2 #17)。
    ch_refresh = ch_sub.add_parser("refresh", help="从 Telegram 同步已加入频道并落库")
    ch_refresh.add_argument("--yes", action="store_true", help="确认执行")
    ch_add = ch_sub.add_parser("add", help="加入监听")
    ch_add.add_argument("channel_ids", type=int, nargs="+", help="Telegram chat_id")
    ch_add.add_argument("--yes", action="store_true", help="确认执行")
    ch_rm = ch_sub.add_parser("remove", help="移出监听")
    ch_rm.add_argument("channel_ids", type=int, nargs="+", help="Telegram chat_id")
    ch_rm.add_argument("--yes", action="store_true", help="确认执行")

    # ---- monitor ----
    mon_p = sub.add_parser("monitor", help="前台启动监听(SIGINT / SIGTERM 退出)")
    mon_p.add_argument(
        "--no-resume",
        action="store_true",
        help="忽略 .env 中 TG_PAUSED=true,强制启动",
    )

    # ---- pause / resume ----
    pause_p = sub.add_parser("pause", help="暂停监听(写 TG_PAUSED=true)")
    _add_json_flag(pause_p)
    resume_p = sub.add_parser("resume", help="恢复监听(写 TG_PAUSED=false)")
    _add_json_flag(resume_p)

    # ---- sync ----
    sync_p = sub.add_parser(
        "sync",
        help="全量同步指定频道(元数据 + 历史消息),完成后退出",
    )
    sync_p.add_argument("channel_ids", type=int, nargs="+", help="Telegram chat_id(空格分隔)")
    sync_p.add_argument("--no-metadata", action="store_true", help="跳过元数据刷新")
    sync_p.add_argument("--no-history", action="store_true", help="跳过历史消息")
    sync_p.add_argument("--resume", action="store_true", help="从 storage 最大 msg_id 之后续拉")
    sync_p.add_argument(
        "--media-policy",
        choices=["metadata", "thumbnail", "full"],
        default="metadata",
        help="媒体下载策略(默认 metadata)",
    )
    sync_p.add_argument(
        "--chat-delay-ms", type=int, default=500, help="频道间 / 单条间隔 ms(默认 500)"
    )
    sync_p.add_argument(
        "--page-delay-ms", type=int, default=1000, help="每 100 条翻页间隔 ms(默认 1000)"
    )

    # ---- messages ----
    msg_p = sub.add_parser("messages", help="消息查询与操作")
    msg_sub = msg_p.add_subparsers(dest="message_cmd", required=True)
    msg_list = msg_sub.add_parser("list", help="查询消息")
    msg_list.add_argument("--channel", type=int, help="限定 chat_id")
    msg_list.add_argument("--search", help="搜索正文 / 文件名")
    msg_list.add_argument("--from", dest="date_from", help="起始日期 YYYY-MM-DD")
    msg_list.add_argument("--to", dest="date_to", help="结束日期 YYYY-MM-DD")
    msg_list.add_argument("--limit", type=int, default=200, help="条数上限(默认 200)")
    msg_list.add_argument("--favorite", action="store_true", help="仅收藏")
    msg_list.add_argument("--tag", help="仅某标签")
    msg_list.add_argument("--pinned", action="store_true", help="仅置顶")
    msg_list.add_argument("--include-unmonitored", action="store_true", help="含非监听频道")
    _add_json_flag(msg_list)
    msg_get = msg_sub.add_parser("get", help="查看单条消息详情")
    msg_get.add_argument("channel_id", type=int)
    msg_get.add_argument("msg_id", type=int)
    _add_json_flag(msg_get)
    msg_fav = msg_sub.add_parser("favorite", help="收藏 / 取消收藏")
    msg_fav.add_argument("channel_id", type=int)
    msg_fav.add_argument("msg_id", type=int)
    msg_fav.add_argument("--off", action="store_true", help="取消收藏")
    msg_tag = msg_sub.add_parser("tag", help="设置标签")
    msg_tag.add_argument("channel_id", type=int)
    msg_tag.add_argument("msg_id", type=int)
    msg_tag.add_argument("--tags", required=True, help="逗号分隔;空串清空")
    msg_note = msg_sub.add_parser("note", help="设置备注")
    msg_note.add_argument("channel_id", type=int)
    msg_note.add_argument("msg_id", type=int)
    msg_note.add_argument("--text", required=True, help="备注文本")
    msg_del = msg_sub.add_parser("delete", help="删除消息")
    msg_del.add_argument("channel_id", type=int)
    msg_del.add_argument("msg_ids", type=int, nargs="+", help="同频道下的消息 ID")
    msg_del.add_argument("--yes", action="store_true", help="确认执行")

    # ---- media ----
    med_p = sub.add_parser("media", help="媒体查询与操作")
    med_sub = med_p.add_subparsers(dest="media_cmd", required=True)
    med_list = med_sub.add_parser("list", help="列出媒体")
    med_list.add_argument("--channel", type=int, help="限定 chat_id")
    med_list.add_argument(
        "--status", choices=["downloading", "done", "failed"], help="下载状态筛选"
    )
    med_list.add_argument("--type", choices=_MEDIA_TYPES, help="媒体类型筛选")
    med_list.add_argument("--search", help="搜索文件名")
    med_list.add_argument("--undownloaded", action="store_true", help="仅未下载(独立于 --status)")
    med_list.add_argument("--sort", choices=["date", "size", "status"], default="date")
    med_list.add_argument("--dir", dest="sort_dir", choices=["asc", "desc"], default="desc")
    med_list.add_argument("--limit", type=int, default=1000)
    med_list.add_argument("--offset", type=int, default=0)
    _add_json_flag(med_list)
    for name in ("download", "retry", "delete"):
        med_x = med_sub.add_parser(name, help=f"媒体 {name}")
        for attr in ("channel_id", "msg_id", "media_idx"):
            med_x.add_argument(attr, type=int)
        if name == "delete":
            med_x.add_argument("--yes", action="store_true", help="确认执行")
    med_dla = med_sub.add_parser("download-all", help="批量下载待下载媒体")
    med_dla.add_argument("--channel", type=int)
    med_dla.add_argument("--type", choices=_MEDIA_TYPES)
    med_dla.add_argument("--yes", action="store_true", help="确认执行")
    med_rec = med_sub.add_parser("reconcile", help="孤儿对账(默认 dry-run)")
    med_rec.add_argument("--apply", action="store_true", help="实际执行清理")
    med_rec.add_argument("--yes", action="store_true", help="确认执行")

    # ---- export ----
    exp_p = sub.add_parser("export", help="导出消息 / 媒体")
    exp_sub = exp_p.add_subparsers(dest="export_cmd", required=True)
    exp_msg = exp_sub.add_parser("messages", help="导出消息")
    exp_msg.add_argument(
        "--format",
        required=True,
        # 2026-11-26:不能在 build_parser 时锁死 choices —— 插件在 bootstrap
        # 才激活,那时 parser 已构造完。改为运行时由 ExporterRegistry 校验,
        # help 里只列内置格式并说明可扩展。
        help=f"导出格式(内置: {', '.join(EXPORTERS.message_formats())};插件可扩展)",
    )
    exp_msg.add_argument("-o", "--out", required=True, help="输出路径")
    exp_msg.add_argument("--channel", type=int, action="append", dest="channel_ids")
    exp_msg.add_argument("--from", dest="date_from", help="起始日期 YYYY-MM-DD")
    exp_msg.add_argument("--to", dest="date_to", help="结束日期 YYYY-MM-DD")
    exp_msg.add_argument("--single", type=int, dest="single_id", help="仅单条消息 ID")
    exp_msg.add_argument("--include-thumbnails", action="store_true")
    exp_msg.add_argument("--no-metadata", action="store_true")
    exp_med = exp_sub.add_parser("media", help="导出媒体列表")
    exp_med.add_argument("--format", required=True, choices=["csv", "zip"])
    exp_med.add_argument("-o", "--out", required=True, help="输出路径")
    exp_med.add_argument("--channel", type=int, dest="channel_id")
    exp_med.add_argument("--status", choices=["downloading", "done", "failed"])
    exp_med.add_argument("--type", choices=_MEDIA_TYPES)
    exp_med.add_argument("--search")
    exp_med.add_argument("--include-thumbnails", action="store_true")

    # ---- config ----
    cfg_p = sub.add_parser("config", help="配置管理与校验")
    cfg_sub = cfg_p.add_subparsers(dest="config_cmd", required=True)
    cfg_show = cfg_sub.add_parser("show", help="展示有效配置(敏感项打码)")
    _add_json_flag(cfg_show)
    cfg_get = cfg_sub.add_parser("get", help="读取单个配置键")
    cfg_get.add_argument("key", help="Settings 字段名(如 media_policy)")
    cfg_set = cfg_sub.add_parser("set", help="设置 + 校验 + 热重载 + 写盘")
    cfg_set.add_argument("key", help="Settings 字段名")
    cfg_set.add_argument("value", help="新值")
    cfg_set.add_argument("--no-validate", action="store_true", help="跳过后端可达性校验")
    cfg_set.add_argument("--yes", action="store_true", help="确认执行")
    cfg_sub.add_parser("validate", help="校验存储 / 对象存储后端可达性")

    # ---- help(2.1.1):打印帮助总览,等同 --help ----
    sub.add_parser("help", help="显示帮助总览")

    return parser


# 子命令分发表(cmd -> (模块, 函数))
_DISPATCH = {
    "sync": ("tgmonitor.cli.sync", "run_sync"),
    "monitor": ("tgmonitor.cli.monitor", "run_monitor"),
    "status": ("tgmonitor.cli.status", "run_status"),
    "login": ("tgmonitor.cli.auth", "run_login"),
    "logout": ("tgmonitor.cli.auth", "run_logout"),
    "channels": ("tgmonitor.cli.channels", "run_channels"),
    "pause": ("tgmonitor.cli.control", "run_pause"),
    "resume": ("tgmonitor.cli.control", "run_resume"),
    "messages": ("tgmonitor.cli.messages", "run_messages"),
    "media": ("tgmonitor.cli.media", "run_media"),
    "export": ("tgmonitor.cli.export", "run_export"),
    "config": ("tgmonitor.cli.config", "run_config"),
}


def main(argv: list[str] | None = None, *, prog: str = "tgmon-cli") -> int:
    """CLI 主入口:解析 argv,路由到子命令实现。

    退出码:0 成功 / 1 业务错误(CliError) / 2 用法错误(argparse) /
    130 KeyboardInterrupt。

    `help` 与无子命令都打印帮助总览;未知参数由 argparse 拒绝(退出码 2)。
    本模块从不加载 Qt,与 GUI 命令 `tgmon` 完全解耦。
    """
    _ensure_utf8_stdio()
    parser = build_parser(prog=prog)
    args = parser.parse_args(argv)
    if args.cmd == "help" or args.cmd not in _DISPATCH:
        parser.print_help()
        return 0

    import importlib

    mod_name, func_name = _DISPATCH[args.cmd]
    run = getattr(importlib.import_module(mod_name), func_name)
    try:
        return asyncio.run(run(args)) or 0
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr, flush=True)
        return 130


def main_tgmon_cli(argv: list[str] | None = None) -> int:
    """`tgmon-cli` console script — 纯 CLI,usage 显示 tgmon-cli。"""
    return main(argv, prog="tgmon-cli")


if __name__ == "__main__":
    sys.exit(main())
