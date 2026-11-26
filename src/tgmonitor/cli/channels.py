"""CLI `channels` 子命令 — 监听频道管理。

list 三种 source / refresh / add / remove。设计见 PRD §5.3。
"""

from __future__ import annotations

import argparse

from tgmonitor.core.app_service import AppService
from tgmonitor.core.monitor.service import MonitorService


async def run_channels(
    args: argparse.Namespace,
    *,
    app: AppService | None = None,
    monitor: MonitorService | None = None,
) -> int:
    """路由到 channels 各子动作。"""
    from tgmonitor.cli._common import CliError, cli_context, emit, info

    async with cli_context(args, app=app, monitor=monitor) as (ctx, _mon, _settings):
        cmd = getattr(args, "channel_cmd", None)

        if cmd == "list":
            source = getattr(args, "source", "monitored")
            if source == "joined":
                channels = await ctx.channels.list_joined_channels()
            elif source == "subscribed":
                channels = await ctx.channels.list_tg_subscribed_channels()
            else:
                channels = await ctx.channels.list_monitored_channels()
            data = [
                {
                    "id": c.id,
                    "title": c.title,
                    "username": c.username,
                    "member_count": c.member_count,
                    "is_monitored": c.is_monitored,
                }
                for c in channels
            ]

            def rows():
                yield "ID / 标题 / username / 成员 / 监听"
                for c in channels:
                    members = c.member_count if c.member_count is not None else "-"
                    yield f"{c.id} / {c.title} / @{c.username or '-'} / {members} / {'是' if c.is_monitored else '否'}"

            emit(data, as_json=getattr(args, "json", False), text_rows=rows())

        elif cmd == "refresh":
            # 写操作(2.1.6 P2 #17):refresh 会把"已在 TG 退出"的频道置
            # tg_subscribed=False 并级联停掉其监听(发 ChannelUnmonitored),
            # 与 add/remove 同一确认边界。
            if not getattr(args, "yes", False):
                raise CliError(
                    "channels refresh 会级联停掉已在 Telegram 退出频道的监听,请加 --yes 确认"
                )
            refreshed = await ctx.channels.refresh_joined_channels()
            emit(
                {"refreshed": len(refreshed)},
                as_json=False,
                text_rows=[f"已同步 {len(refreshed)} 个频道"],
            )

        elif cmd == "add":
            if not getattr(args, "yes", False):
                raise CliError("channels add 会修改监听白名单,请加 --yes 确认")
            added, skipped = 0, 0
            for cid in args.channel_ids:
                ch = await ctx.storage.get_channel(cid)
                if ch is None:
                    info(f"跳过 {cid}:本地无此频道(先 channels refresh)")
                    skipped += 1
                    continue
                await ctx.channels.monitor_channel(ch)
                added += 1
            emit(
                {"added": added, "skipped": skipped},
                as_json=False,
                text_rows=[f"加入监听: {added} 成功 / {skipped} 跳过"],
            )

        elif cmd == "remove":
            if not getattr(args, "yes", False):
                raise CliError("channels remove 会修改监听白名单,请加 --yes 确认")
            for cid in args.channel_ids:
                await ctx.channels.unmonitor_channel(cid)
            emit(
                {"removed": len(args.channel_ids)},
                as_json=False,
                text_rows=[f"移出监听: {len(args.channel_ids)}"],
            )

        else:
            raise CliError("用法:tgmonitor channels {list|refresh|add|remove} ...")

    return 0
