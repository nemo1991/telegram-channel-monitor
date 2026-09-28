"""CLI `monitor` 子命令:bootstrap → 启动 monitor → SIGINT 等待 → shutdown。

2026-09-27 refactor:CLI 第一批子命令之一,headless 守护模式运行,
退出码 0(SIGINT 干净退出) / 1(bootstrap 未 ready)。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from tgmonitor.core.app_service import AppService
from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.core.runtime import bootstrap, shutdown

log = logging.getLogger(__name__)


async def run_monitor(
    args: argparse.Namespace,
    *,
    app: AppService | None = None,
    monitor: MonitorService | None = None,
) -> int:
    """CLI monitor 主流程:前台跑 monitor 到 SIGINT / SIGTERM,然后干净退出。

    与 GUI 路径差异:
    - 无 UI 状态指示,只走 stdout print
    - 启动后从 storage 重载白名单(与 GUI 行为一致)
    - 暂停状态走 `app.is_paused`;`--no-resume` 标志忽略暂停强制启动
      (CLI 假定用户用此标志是明确知道后果的;GUI 没有此能力)
    - SIGINT / SIGTERM 都接 `loop.add_signal_handler`,触发 asyncio.Event
      让 `await stop.wait()` 醒来,然后进入 finally → shutdown

    `app` / `monitor` 入参:测试用,允许预构 AppService + MonitorService 注入
    fakes / stub。`None` 时走 `runtime.bootstrap()` 真实装配(生产路径)。
    """
    log.info("[cli.monitor] starting")
    own_bootstrap = app is None or monitor is None
    if own_bootstrap:
        app, monitor, settings, _ = await bootstrap()
    assert app is not None and monitor is not None
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        log.info("SIGINT/SIGTERM received, stopping monitor")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, RuntimeError):
            # 部分平台不支持(如 Windows 的某些信号);忽略
            pass

    try:
        state, detail = await app.bootstrap()
        if state != "ready":
            print(
                f"[cli.monitor] bootstrap state={state};请先在 GUI 完成登录后重试",
                file=sys.stderr,
            )
            return 1

        # 默认行为:从 storage 重载白名单(与 GUI 一致)
        subscribed = await app.storage.list_subscribed_channels()
        monitor.set_whitelist(c.id for c in subscribed)

        # --no-resume 标志:覆盖 settings.paused=True 强制启动 monitor
        if app.is_paused and not args.no_resume:
            print(
                "[cli.monitor] settings.paused=true;skipped. 用 --no-resume 强制启动",
                file=sys.stderr,
            )
            return 1

        if app.is_paused and args.no_resume:
            log.warning("[cli.monitor] --no-resume: 强制启动(忽略 .env 中 TG_PAUSED=true)")

        print(
            f"[cli.monitor] subscribed={len(subscribed)} channels;starting…",
            flush=True,
        )

        await monitor.start()
        await stop.wait()
        print("[cli.monitor] stopped", flush=True)
        return 0
    finally:
        # remove_signal_handler 走 try/except — Windows 上 add 失败的同样 remove 失败
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        if own_bootstrap:
            await shutdown(app, monitor)
