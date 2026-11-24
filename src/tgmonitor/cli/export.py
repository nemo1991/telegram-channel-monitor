"""CLI `export` 子命令 — 导出消息 / 媒体。

messages 6 格式 / media csv+zip。async for 驱动 ExportService 进度,
订阅 ExportDone 取结果。设计见 PRD §5.8。
"""

from __future__ import annotations

import argparse

from tgmonitor.core.app_service import AppService
from tgmonitor.core.dto import (
    ExportFormat,
    ExportRequest,
    MediaDownloadStatus,
    MediaExportRequest,
    MediaType,
)
from tgmonitor.core.events import ExportDone
from tgmonitor.core.monitor.service import MonitorService


async def run_export(
    args: argparse.Namespace,
    *,
    app: AppService | None = None,
    monitor: MonitorService | None = None,
) -> int:
    """路由到 export messages / media。"""
    from tgmonitor.cli._common import CliError, cli_context, emit_json, parse_date, progress

    async with cli_context(args, app=app, monitor=monitor) as (ctx, _mon, _settings):
        cmd = getattr(args, "export_cmd", None)

        done_result: dict = {}
        done_error: list[str] = []

        async def _on_done(e: ExportDone) -> None:
            if e.error:
                done_error.append(e.error)
            elif e.result is not None:
                done_result["out_path"] = e.result.out_path
                done_result["message_count"] = e.result.message_count
                done_result["bytes_written"] = e.result.bytes_written

        ctx.bus.subscribe(ExportDone, _on_done)

        if cmd == "messages":
            date_from = parse_date(args.date_from) if getattr(args, "date_from", None) else None
            date_to = parse_date(args.date_to) if getattr(args, "date_to", None) else None
            request = ExportRequest(
                channel_ids=list(getattr(args, "channel_ids", None) or []),
                date_from=date_from,
                date_to=date_to,
                format=ExportFormat(args.format),
                out_path=args.out,
                include_metadata=not getattr(args, "no_metadata", False),
                include_thumbnails=getattr(args, "include_thumbnails", False),
                single_message_id=getattr(args, "single_id", None),
            )
            stream = ctx.export(request)

        elif cmd == "media":
            fmt = args.format
            if fmt == "zip":
                request2 = ExportRequest(
                    channel_ids=[args.channel_id] if getattr(args, "channel_id", None) else [],
                    format=ExportFormat.ZIP,
                    out_path=args.out,
                    include_thumbnails=getattr(args, "include_thumbnails", False),
                )
                stream = ctx.export(request2)
            else:  # csv
                media_request = MediaExportRequest(
                    channel_id=getattr(args, "channel_id", None),
                    status=MediaDownloadStatus(args.status)
                    if getattr(args, "status", None)
                    else None,
                    media_type=MediaType(args.type) if getattr(args, "type", None) else None,
                    search=getattr(args, "search", "") or "",
                    format=ExportFormat.MEDIA_CSV,
                    out_path=args.out,
                )
                stream = ctx.export_media_list(media_request)
        else:
            raise CliError("用法:tgmonitor export {messages|media} ...")

        written = 0
        async for _ in stream:
            written += 1
        await ctx.bus.flush()

        if done_error:
            raise CliError(f"导出失败: {done_error[0]}")
        if not done_result:
            # 老后端桩可能不发 ExportDone — 用 out_path 兜底
            done_result = {"out_path": args.out, "message_count": None, "bytes_written": None}
        progress(f"导出心跳 {written} 次")
        emit_json(done_result)

    return 0
