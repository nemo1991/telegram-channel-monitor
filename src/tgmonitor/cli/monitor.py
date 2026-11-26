"""CLI `monitor` 子命令:参数化 start → SIGINT 等待 → shutdown。

2026-09-27 refactor:CLI 第一批子命令之一,headless 守护模式运行,
退出码 0(SIGINT 干净退出) / 1(start 未 ready 或用户暂停)。
2026-11-26 ADR-002:步骤顺序由 `AppService.start()` 统一为白名单 → monitor →
bootstrap;本模块只保留 CLI 策略(paused 提示 / 退出码 / stderr 输出)。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from tgmonitor.core.app_service import AppService, StartOptions
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
    - 无 UI 状态指示,状态行只走 stderr print(stdout 只数据;本命令无数据输出)
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
    # 测试注入路径的 app 可能未装配 monitor(生产路径 runtime.bootstrap 已注入);
    # start() 走 app.monitor,这里对齐引用,避免白名单 / 实时流写到不同实例。
    if app.monitor is not monitor:
        app.monitor = monitor
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
        # 启动进度在 start() 之前输出 — start() 中途抛错时用户仍看得到进入启动
        print("[cli.monitor] starting…", file=sys.stderr, flush=True)
        result = await app.start(StartOptions(respect_paused=not args.no_resume))
        if result.state == "paused":
            print(
                "[cli.monitor] settings.paused=true;skipped. 用 --no-resume 强制启动",
                file=sys.stderr,
            )
            return 1
        if result.state != "ready":
            # 统一顺序下 monitor 先于 bootstrap 启动;CLI 失败退出前显式停掉,
            # 否则注入 app/monitor 的调用路径不走 finally 的 shutdown 会残留 worker。
            if result.monitor_started:
                try:
                    await monitor.stop()
                except Exception:
                    log.exception("[cli.monitor] rollback monitor.stop failed")
            print(
                f"[cli.monitor] bootstrap state={result.state};请先在 GUI 完成登录后重试",
                file=sys.stderr,
            )
            return 1

        # --no-resume 标志:覆盖 settings.paused=True 强制启动(策略仍在调用方)
        if app.is_paused and args.no_resume:
            log.warning("[cli.monitor] --no-resume: 强制启动(忽略 .env 中 TG_PAUSED=true)")

        print(
            f"[cli.monitor] subscribed={result.whitelist_count} channels;listening…",
            file=sys.stderr,
            flush=True,
        )

        await stop.wait()
        print("[cli.monitor] stopped", file=sys.stderr, flush=True)
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
