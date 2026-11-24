"""CLI `status` 子命令:登录/监听状态 + 统计 + 后端信息。

status 是诊断命令:未登录时**不报错**,仍输出实际状态,退出码 0。
2.1.0 设计见 `docs/PRD-2.1.0-CLI.md` §5.1。
"""

from __future__ import annotations

import argparse

from tgmonitor.core.app_service import AppService
from tgmonitor.core.dto import ChannelStats, MediaDownloadStatus
from tgmonitor.core.monitor.service import MonitorService


async def run_status(
    args: argparse.Namespace,
    *,
    app: AppService | None = None,
    monitor: MonitorService | None = None,
) -> int:
    """输出状态与统计,始终返回 0。"""
    from tgmonitor.cli._common import cli_context, emit

    async with cli_context(args, app=app, monitor=monitor) as (ctx, _mon, _settings):
        state = await ctx.auth.get_login_state()
        paused = ctx.is_paused
        monitored = await ctx.channels.list_monitored_channels()
        subscribed = await ctx.channels.list_tg_subscribed_channels()

        # 统计:逐频道 count_messages 累加 + 全局 count_media
        channel_ids = [c.id for c in subscribed] or [c.id for c in monitored]
        msg_total = 0
        for cid in channel_ids:
            msg_total += await ctx.storage.count_messages(cid)
        media_total = await ctx.storage.count_media()
        media_done = await ctx.storage.count_media(status=MediaDownloadStatus.DONE)

        settings = ctx.settings
        data = {
            "login_state": state,
            "paused": paused,
            "monitor": {"monitored": len(monitored), "subscribed": len(subscribed)},
            "backends": {
                "storage": settings.db_backend.value,
                "objectstore": settings.objectstore_backend.value,
            },
            "stats": {
                "messages": msg_total,
                "media": media_total,
                "media_done": media_done,
            },
        }

        # 可选逐频道明细
        channel_stats: dict[int, ChannelStats] = {}
        if getattr(args, "channels", False) and channel_ids:
            channel_stats = await ctx.storage.aggregate_per_channel(channel_ids)
            data["channel_stats"] = {str(k): vars(v) for k, v in channel_stats.items()}

        def rows():
            yield f"登录: {state}"
            yield f"监听: {'已暂停' if paused else '运行中'} ({len(monitored)}/{len(subscribed)} 频道)"
            yield f"后端: {settings.db_backend.value} / 对象存储: {settings.objectstore_backend.value}"
            yield f"统计: 消息 {msg_total} · 媒体 {media_total} (已下载 {media_done})"
            for cid, st in channel_stats.items():
                yield f"  - {cid}: {vars(st)}"

        emit(data, as_json=getattr(args, "json", False), text_rows=rows())
    return 0
