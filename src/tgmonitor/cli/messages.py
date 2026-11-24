"""CLI `messages` 子命令 — 消息查询与单条操作。

list/get/favorite/tag/note/delete。设计见 PRD §5.6。
"""

from __future__ import annotations

import argparse

from tgmonitor.core.app_service import AppService
from tgmonitor.core.monitor.service import MonitorService


async def run_messages(
    args: argparse.Namespace,
    *,
    app: AppService | None = None,
    monitor: MonitorService | None = None,
) -> int:
    """路由到 messages 各子动作。"""
    from tgmonitor.cli._common import (
        CliError,
        cli_context,
        emit,
        parse_csv_list,
        parse_date,
    )

    async with cli_context(args, app=app, monitor=monitor) as (ctx, _mon, _settings):
        cmd = getattr(args, "message_cmd", None)

        if cmd == "list":
            date_from = parse_date(args.date_from) if getattr(args, "date_from", None) else None
            date_to = parse_date(args.date_to) if getattr(args, "date_to", None) else None
            msgs = await ctx.messages.list_messages(
                channel_ids=[args.channel] if getattr(args, "channel", None) else None,
                date_from=date_from,
                date_to=date_to,
                limit=getattr(args, "limit", 200),
                search=getattr(args, "search", "") or "",
                include_unmonitored=getattr(args, "include_unmonitored", False),
                favorite_only=getattr(args, "favorite", False),
                tag_only=bool(getattr(args, "tag", None)),
                pinned_only=getattr(args, "pinned", False),
            )
            # tag_only 需要额外按 tag 值过滤(list_messages 的 tag_only 是"有任意标签")
            if getattr(args, "tag", None):
                msgs = [m for m in msgs if args.tag in m.tags]

            def rows():
                for m in msgs:
                    summary = (m.text or "").replace("\n", " ")[:60]
                    yield f"{m.channel_id}/{m.telegram_msg_id} / {m.date.isoformat()} / {summary}"

            emit(
                [_msg_dto(m) for m in msgs], as_json=getattr(args, "json", False), text_rows=rows()
            )

        elif cmd == "get":
            msg = await ctx.storage.get_message(args.channel_id, args.msg_id)
            if msg is None:
                raise CliError(f"消息不存在: {args.channel_id}/{args.msg_id}")
            emit(
                _msg_dto(msg),
                as_json=getattr(args, "json", False),
                text_rows=[
                    f"{msg.channel_id}/{msg.telegram_msg_id} {msg.date.isoformat()}",
                    msg.text,
                ],
            )

        elif cmd == "favorite":
            await ctx.messages.set_favorite(args.channel_id, args.msg_id, not args.off)
            emit(
                {"ok": True, "favorite": not args.off},
                as_json=getattr(args, "json", False),
                text_rows=[f"收藏: {'已设' if not args.off else '已取消'}"],
            )

        elif cmd == "tag":
            tags = parse_csv_list(args.tags)
            await ctx.messages.set_tags(args.channel_id, args.msg_id, tags)
            emit(
                {"ok": True, "tags": tags},
                as_json=getattr(args, "json", False),
                text_rows=[f"标签: {','.join(tags) or '(清空)'}"],
            )

        elif cmd == "note":
            await ctx.messages.set_notes(args.channel_id, args.msg_id, args.text)
            emit(
                {"ok": True, "notes": args.text},
                as_json=getattr(args, "json", False),
                text_rows=["备注已更新"],
            )

        elif cmd == "delete":
            if not getattr(args, "yes", False):
                raise CliError("messages delete 不可逆,请加 --yes 确认")
            for mid in args.msg_ids:
                await ctx.messages.delete_message(args.channel_id, mid)
            emit(
                {"ok": True, "deleted": len(args.msg_ids)},
                as_json=getattr(args, "json", False),
                text_rows=[f"已删除 {len(args.msg_ids)} 条消息"],
            )

        else:
            raise CliError("用法:tgmonitor messages {list|get|favorite|tag|note|delete} ...")

    return 0


def _msg_dto(m) -> dict:
    """MessageDTO → 精简 dict(JSON 输出)。"""
    return {
        "channel_id": m.channel_id,
        "msg_id": m.telegram_msg_id,
        "date": m.date.isoformat() if m.date else None,
        "author": m.author,
        "text": m.text,
        "views": m.views,
        "is_pinned": m.is_pinned,
        "is_favorite": m.is_favorite,
        "tags": m.tags,
        "notes": m.notes,
        "media_count": len(m.media),
    }
