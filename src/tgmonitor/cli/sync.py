"""CLI `sync` 子命令:bootstrap → 加载已订阅 → sync_channels → 进度打印 → shutdown。

2026-09-27 refactor:CLI 第一批子命令之一,与 GUI 共用 `core.runtime` 装配。
"""

from __future__ import annotations

import argparse
import logging
import sys

from tgmonitor.core.app_service import AppService
from tgmonitor.core.config import MediaPolicy
from tgmonitor.core.dto import SyncOptions
from tgmonitor.core.events import ChannelSyncProgress
from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.core.runtime import bootstrap, shutdown

log = logging.getLogger(__name__)


async def run_sync(
    args: argparse.Namespace,
    *,
    app: AppService | None = None,
    monitor: MonitorService | None = None,
) -> int:
    """CLI sync 主流程:阻塞跑完一轮 sync,输出进度到 stdout,退出码 0/1。

    与 GUI 路径差异:
    - 进度反馈走 stdout `print`(无 UI dialog)
    - 白名单以 `args.channel_ids` 为准(不走 storage `list_subscribed_channels()`,
      CLI 显式选择胜过默认行为)
    - bootstrap 未 ready 时退出码 1 + 提示去 GUI 完成登录(CLI 没有 code 输入面板)

    `app` / `monitor` 入参:测试用,允许预构 AppService + MonitorService 注入
    fakes / stub。`None` 时走 `runtime.bootstrap()` 真实装配(生产路径)。
    """
    log.info("[cli.sync] starting: channel_ids=%s", args.channel_ids)
    own_bootstrap = app is None or monitor is None
    if own_bootstrap:
        app, monitor, settings, _ = await bootstrap()
    assert app is not None and monitor is not None
    try:
        # CLI 假定 .env + session 有效;启动后未 ready → 提示去 GUI
        state, detail = await app.bootstrap()
        if state != "ready":
            print(
                f"[cli.sync] bootstrap state={state} detail={detail};请先在 GUI 完成登录后重试",
                file=sys.stderr,
            )
            return 1

        # 白名单 = CLI 显式指定的 channel_ids
        monitor.set_whitelist(args.channel_ids)

        # 进度事件 → stdout
        async def on_progress(e: ChannelSyncProgress) -> None:
            print(
                f"[sync] cid={e.channel_id} stage={e.stage} {e.detail}",
                flush=True,
            )

        app.bus.subscribe(ChannelSyncProgress, on_progress)

        options = SyncOptions(
            include_metadata=not args.no_metadata,
            include_history=not args.no_history,
            resume_from_saved=args.resume,
            chat_delay_ms=args.chat_delay_ms,
            page_delay_ms=args.page_delay_ms,
        )
        # CLI `--media-policy` 走 `sync_channels(media_policy=)` 一次性 override
        # — 不污染 service 状态,下次启动按 .env 默认
        result = await app.sync_channels(
            args.channel_ids,
            options,
            media_policy=MediaPolicy(args.media_policy),
        )
        print(
            f"[sync] done: {result.total_messages_added} messages added "
            f"(cancelled={result.cancelled})",
            flush=True,
        )
        # rate_limited 透传警告
        if result.rate_limited_seconds is not None:
            print(
                f"[sync] warning: hit FLOOD_WAIT, backed off {result.rate_limited_seconds:.0f}s",
                file=sys.stderr,
            )
            return 1
        return 0
    finally:
        if own_bootstrap:
            await shutdown(app, monitor)