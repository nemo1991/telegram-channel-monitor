"""App composition root + UI 启动(qasync 事件循环)。

唯一启动入口 `run()`;装配顺序:
    Settings → EventBus → Storage(connect + init_schema)
                    → ObjectStore(connect)
                    → TelegramClient(aiotdlib or fake)
                    → MonitorService
                    → AppService
                    → UI(QMainWindow)
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from tgmonitor.core.app_service import AppService
from tgmonitor.core.config import Settings
from tgmonitor.core.events import EventBus
from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.core.objectstore.factory import build_object_store
from tgmonitor.core.storage.factory import build_storage
from tgmonitor.core.telegram.factory import build_telegram_client

log = logging.getLogger(__name__)


async def _bootstrap() -> tuple[AppService, MonitorService, Settings]:
    settings = Settings()  # type: ignore[call-arg]
    settings.ensure_dirs()

    bus = EventBus()
    storage = build_storage(settings)
    await storage.connect()
    await storage.init_schema()

    objects = build_object_store(settings)
    await objects.connect()

    # 默认尝试 aiotdlib;失败回退 fake(开发/CI 无凭据也能跑)
    client = build_telegram_client(settings, use_fake=False)
    monitor = MonitorService(bus, client, storage, objects, settings)
    app = AppService(bus, client, storage, objects, settings)
    return app, monitor, settings


def run() -> None:
    """启动 GUI。

    事件循环模式:
      step 1) `loop.run_until_complete(_setup_async)` — 同步阻塞做 async 装配
      step 2) `show window` — UI 显示
      step 3) `loop.run_forever()` — Qt + asyncio 共跑,直到触发 aboutToQuit
      step 4) aboutToQuit 钩子上挂的 `_shutdown_then_quit` 先跑 async 清理,再真 quit
              (必须在 loop 还活着时跑完,否则 'with loop' 退出会 close 掉 loop)

    不要在 async 协程里 `await loop.run_forever()` —— 会被 "Event loop already running" 拒。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # qasync 让 Qt 跑在 asyncio 事件循环上
    try:
        from PySide6.QtWidgets import QApplication
        from qasync import QEventLoop
    except ImportError as e:  # pragma: no cover
        print("缺少 PySide6 / qasync,请 `pip install -e .[all]`", file=sys.stderr)
        raise SystemExit(1) from e

    qt_app = QApplication.instance() or QApplication(sys.argv)
    loop = QEventLoop(qt_app)
    asyncio.set_event_loop(loop)

    # 容器:由 step1 填充,step4 消费
    state: dict[str, object] = {}

    async def _setup_async() -> None:
        app_svc, monitor, settings = await _bootstrap()
        # 启动 monitor
        subscribed = await app_svc.storage.list_channels()
        monitor.set_whitelist(c.id for c in subscribed)
        await monitor.start()
        state["app"] = app_svc
        state["monitor"] = monitor
        state["settings"] = settings

    async def _shutdown_async() -> None:
        monitor = state.get("monitor")  # type: ignore[assignment]
        app_svc = state.get("app")      # type: ignore[assignment]
        if monitor is not None:
            try:
                await monitor.stop()
            except Exception:  # noqa: BLE001
                log.exception("monitor.stop() failed")
        if app_svc is not None:
            try:
                await app_svc.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("app.shutdown() failed")

    # step 1: 同步阻塞做 async 装配(loop 此时尚未运行)
    loop.run_until_complete(_setup_async())

    app_svc: AppService = state["app"]          # type: ignore[assignment]
    monitor: MonitorService = state["monitor"]  # type: ignore[assignment]
    settings: Settings = state["settings"]      # type: ignore[assignment]

    # UI
    from tgmonitor.ui.main_window import MainWindow

    env_path = Path(".env").resolve()
    win = MainWindow(app_svc, monitor, loop, env_path=env_path)
    win.show()

    # 退出钩子:任何路径触发 quit(关窗 / SIGINT)→ **先异步清理** → 再真 quit
    # 这样 step 4 的 async 任务在 loop 仍然 alive 时跑完,避开 'Event loop is closed'。
    def _shutdown_then_quit() -> None:
        async def _do_shutdown_then_quit() -> None:
            try:
                await _shutdown_async()
            finally:
                qt_app.quit()
        try:
            fut = asyncio.ensure_future(_do_shutdown_then_quit())
        except RuntimeError:
            # loop 已关(罕见):尽力清理后退出
            log.warning("loop already closed, skipping async shutdown")
            qt_app.quit()
            return

        def _on_done(f: "asyncio.Future[None]") -> None:
            if f.cancelled():
                return
            exc = f.exception()
            if exc is not None:
                log.exception("shutdown failed: %s", exc)

        fut.add_done_callback(_on_done)

    qt_app.aboutToQuit.connect(_shutdown_then_quit)

    # 信号:从任意线程触发 asyncio 的 quit
    def _on_signal(*_: object) -> None:
        log.info("signal received, shutting down…")
        qt_app.quit()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, RuntimeError):
            # 部分平台不支持(如 Windows 的某些信号);忽略
            pass

    # step 2 + 3: 跑事件循环(同时消化 Qt 信号 与 asyncio 任务)
    with loop:
        loop.run_forever()
    # 此处 loop 已被 QEventLoop.__exit__ close,async 任务保证在退出前完成
