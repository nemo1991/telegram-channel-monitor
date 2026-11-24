"""CLI `pause` / `resume` 子命令 — 监听暂停/恢复。

pause/resume 语义在 AppService 层(非 MonitorService),会写 .env 的
TG_PAUSED 并维护 client / monitor 生命周期。设计见 PRD §5.4。
"""

from __future__ import annotations

import argparse

from tgmonitor.core.app_service import AppService
from tgmonitor.core.monitor.service import MonitorService


async def run_pause(
    args: argparse.Namespace,
    *,
    app: AppService | None = None,
    monitor: MonitorService | None = None,
) -> int:
    """暂停监听。"""
    from tgmonitor.cli._common import cli_context, emit, info

    async with cli_context(args, app=app, monitor=monitor) as (ctx, _mon, _settings):
        await ctx.pause_monitor(source="cli")
        emit({"paused": True}, as_json=getattr(args, "json", False), text_rows=["监听已暂停"])
        if not getattr(args, "json", False):
            info("")
    return 0


async def run_resume(
    args: argparse.Namespace,
    *,
    app: AppService | None = None,
    monitor: MonitorService | None = None,
) -> int:
    """恢复监听。client.start 失败时 AppService 不改 .env / 不发事件。"""
    from tgmonitor.cli._common import CliError, cli_context, emit

    async with cli_context(args, app=app, monitor=monitor) as (ctx, _mon, _settings):
        await ctx.resume_monitor(source="cli")
        if ctx.is_paused:
            raise CliError("恢复失败:Telegram 客户端未能启动( session / 凭据失效?)")
        emit({"paused": False}, as_json=getattr(args, "json", False), text_rows=["监听已恢复"])
    return 0
