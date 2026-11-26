"""CLI `media` 子命令 — 媒体查询与操作。

list/download/download-all/retry/delete/reconcile。设计见 PRD §5.7。
"""

from __future__ import annotations

import argparse

from tgmonitor.core.app_service import AppService
from tgmonitor.core.dto import MediaDownloadStatus, MediaType, SortDir, SortKey
from tgmonitor.core.monitor.service import MonitorService


async def run_media(
    args: argparse.Namespace,
    *,
    app: AppService | None = None,
    monitor: MonitorService | None = None,
) -> int:
    """路由到 media 各子动作。"""
    from tgmonitor.cli._common import CliError, cli_context, emit

    async with cli_context(args, app=app, monitor=monitor) as (ctx, _mon, _settings):
        cmd = getattr(args, "media_cmd", None)

        if cmd == "list":
            rows, total = await ctx.media.list_media(
                channel_id=getattr(args, "channel", None),
                status=_status(args.status) if getattr(args, "status", None) else None,
                media_type=_mediatype(args.type) if getattr(args, "type", None) else None,
                search=getattr(args, "search", "") or "",
                undownloaded=getattr(args, "undownloaded", False),
                limit=getattr(args, "limit", 1000),
                offset=getattr(args, "offset", 0),
                sort=SortKey(getattr(args, "sort", "date")),
                sort_dir=SortDir(getattr(args, "sort_dir", "desc")),
            )

            def text_rows():
                yield "channel/msg/idx / 类型 / 状态 / 文件名"
                for msg, idx, media in rows:
                    status = media.download_status or "未下载"
                    yield (
                        f"{msg.channel_id}/{msg.telegram_msg_id}/{idx} / "
                        f"{media.type.value} / {status} / {media.file_name or '-'}"
                    )
                yield f"共 {total} 条"

            data = {
                "items": [
                    {
                        "channel_id": msg.channel_id,
                        "msg_id": msg.telegram_msg_id,
                        "media_idx": idx,
                        "type": media.type.value,
                        "status": media.download_status,
                        "file_name": media.file_name,
                        "file_size": media.file_size,
                    }
                    for msg, idx, media in rows
                ],
                "total": total,
            }
            emit(data, as_json=getattr(args, "json", False), text_rows=text_rows())

        elif cmd == "download":
            await ctx.media.download_media(args.channel_id, args.msg_id, args.media_idx)
            emit(
                {"ok": True},
                as_json=False,
                text_rows=[f"已发起下载 {args.channel_id}/{args.msg_id}/{args.media_idx}"],
            )

        elif cmd == "download-all":
            if not getattr(args, "yes", False):
                raise CliError("media download-all 会批量下载,请加 --yes 确认")
            n = await ctx.media.download_all_pending(
                channel_id=getattr(args, "channel", None),
                media_type=_mediatype(args.type) if getattr(args, "type", None) else None,
            )
            emit(
                {"started": n},
                as_json=False,
                text_rows=[f"已发起 {n} 个下载"],
            )

        elif cmd == "retry":
            await ctx.media.retry_media(args.channel_id, args.msg_id, args.media_idx)
            emit(
                {"ok": True},
                as_json=False,
                text_rows=[f"已重试 {args.channel_id}/{args.msg_id}/{args.media_idx}"],
            )

        elif cmd == "delete":
            if not getattr(args, "yes", False):
                raise CliError("media delete 不可逆,请加 --yes 确认")
            await ctx.media.delete_media(args.channel_id, args.msg_id, args.media_idx)
            emit(
                {"ok": True},
                as_json=False,
                text_rows=[f"已删除 {args.channel_id}/{args.msg_id}/{args.media_idx}"],
            )

        elif cmd == "reconcile":
            apply = getattr(args, "apply", False)
            if apply and not getattr(args, "yes", False):
                raise CliError("media reconcile --apply 会实际删除孤儿,请加 --yes 确认")
            result = await ctx.media.reconcile_orphans(dry_run=not apply)
            data = {
                "backend": result.backend,
                "scanned": result.scanned,
                "referenced": result.referenced,
                "orphans": result.orphans,
                "deleted": result.deleted,
                "dry_run": result.dry_run,
            }
            mode = "预览(dry-run)" if result.dry_run else "已执行"
            emit(
                data,
                as_json=False,
                text_rows=[
                    f"孤儿对账[{mode}]: 扫描 {result.scanned} / 引用 {result.referenced} / "
                    f"孤儿 {result.orphans} / 删除 {result.deleted}"
                ],
            )

        else:
            raise CliError(
                "用法:tgmon-cli media {list|download|download-all|retry|delete|reconcile} ..."
            )

    return 0


def _status(value: str) -> MediaDownloadStatus:
    return MediaDownloadStatus(value)


def _mediatype(value: str) -> MediaType:
    return MediaType(value)
