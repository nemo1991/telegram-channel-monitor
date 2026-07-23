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
import time

from tgmonitor.core.app_service import AppService
from tgmonitor.core.config import Settings
from tgmonitor.core.events import EventBus
from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.core.objectstore.factory import build_object_store
from tgmonitor.core.storage.factory import build_storage
from tgmonitor.core.telegram.factory import build_telegram_client

log = logging.getLogger(__name__)


async def _bootstrap() -> tuple[AppService, MonitorService, Settings]:
    t0 = time.monotonic()
    settings = Settings()  # type: ignore[call-arg]
    # v1.0.1:Settings 的 Path defaults 已经是 platform-native 绝对路径
    # (~/Library/Application Support/tgmonitor/...),不再需要 .resolve()
    # 把相对路径强制绝对 — 之前这步是 cwd-relative 的根因。
    settings.ensure_dirs()
    log.info(
        "[bootstrap] settings loaded in %.2fs | data_dir=%s session=%s exists=%s | db_backend=%s",
        time.monotonic() - t0,
        settings.data_root,
        settings.session_dir,
        (settings.session_dir / "tdlib").exists(),
        settings.db_backend.value,
    )

    bus = EventBus()

    t = time.monotonic()
    storage = build_storage(settings)
    await storage.connect()
    log.info("[bootstrap] storage.connect() took %.2fs", time.monotonic() - t)
    t = time.monotonic()
    await storage.init_schema()
    log.info("[bootstrap] storage.init_schema() took %.2fs", time.monotonic() - t)

    t = time.monotonic()
    objects = build_object_store(settings)
    await objects.connect()
    log.info(
        "[bootstrap] objectstore.connect() took %.2fs backend=%s",
        time.monotonic() - t, settings.objectstore_backend.value,
    )

    t = time.monotonic()
    # 默认尝试 aiotdlib;失败回退 fake(开发/CI 无凭据也能跑)
    client = build_telegram_client(settings, use_fake=False, event_bus=bus)
    log.info(
        "[bootstrap] telegram client built in %.2fs kind=%s",
        time.monotonic() - t, type(client).__name__,
    )

    monitor = MonitorService(bus, client, storage, objects, settings)
    app = AppService(bus, client, storage, objects, settings)
    log.info(
        "[bootstrap] full bootstrap done in %.2fs",
        time.monotonic() - t0,
    )
    return app, monitor, settings


def run() -> None:
    """启动 GUI。

    事件循环模式(单 loop 持续运行,绝不暂停):
      step 0) 创建 qasync `QEventLoop`,set 为当前事件循环
      step 1) 用 `asyncio.ensure_future` 把 `_setup_then_show` 调度到该 loop
              — 此时 loop 尚未 `run_forever`,但 Task 已绑定到正确 loop 上
      step 2) `aboutToQuit` 信号 + 信号处理 + `qt_app.exec` 都不需要;
              改用 `with loop: loop.run_forever()` 跑 Qt+asyncio 共循环
      step 3) `_setup_then_show` 在 loop 内与 Qt 事件交错执行:async 装配 → UI 构造 → window.show
      step 4) aboutToQuit 钩子挂的 `_shutdown_then_quit` 先跑 async 清理 → 然后 qt_app.quit

    **关键区别 — 取消 `loop.run_until_complete`**:
    旧版用 `loop.run_until_complete(_setup_async)` 再 `run_forever()`,中间
    qasync 的 `__is_running` 被设为 False,asyncio `_set_running_loop(None)`,
    Tasks 处于 paused 状态。aiotdlib 内部 thread 在这段窗口发 IO wakeup 时,
    `Task.__step()` 检查 "loop is the running loop" 失败,抛 `RuntimeError:
    loop ... is not the running loop`,日志刷「qasync._QEventLoop: Exception in
    callback Task.task_wakeup()」。

    新版用单 `run_forever()` + `ensure_future`,loop 始终 running,这窗口不复存在,
    根因消除。

    不要在协程里 `await loop.run_forever()` —— 会撞 "Event loop already running"。
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

    # 应用图标(macOS dock / 任务栏 / 任务管理器)
    # PySide6 没有 setApplicationIcon,用 QGuiApplication.setWindowIcon(静态)。
    # 它会影响所有未单独设置 icon 的窗口(包括 MainWindow)。
    from PySide6.QtGui import QGuiApplication

    from tgmonitor.ui.icon import load_app_icon
    QGuiApplication.setWindowIcon(load_app_icon())

    # 全局 QSS — 字号 / 间距 / 状态色(由 ThemeManager 统一管理)
    try:
        # 读 TG_THEME 环境变量决定启动主题(默认 LIGHT)
        import os as _os

        from tgmonitor.ui.theme import Theme, ThemeManager
        env_theme = _os.environ.get("TG_THEME", "light").lower()
        start_theme = Theme.DARK if env_theme == "dark" else Theme.LIGHT
        ThemeManager.apply(start_theme)
    except Exception:  # noqa: BLE001
        log.warning("failed to load theme; falling back to default")

    # 容器:由 setup_then_show 填充,shutdown 时消费
    state: dict[str, object] = {}
    setup_failed: list[BaseException] = []

    # `.env` 解析:同步 I/O,放 loop 外,不阻塞 qasync 的事件循环。
    # v1.0.1:走 platform-native 目录(macOS ~/Library/Application Support/
    # tgmonitor/.env 等),Settings.model_config.env_file 同源 — 不依赖 cwd。
    from tgmonitor.core.config import _user_data_dir
    env_path = _user_data_dir() / ".env"

    async def _setup_then_show() -> None:
        """一次性做完:async 装配 → MainWindow 构造 → window.show()。

        整个跑在 qasync 的 loop 上,与 Qt 事件交错。这样 loop 始终 running,
        彻底去掉旧 `run_until_complete` + `run_forever` 中间的 paused 窗口。
        """
        try:
            t_setup = time.monotonic()
            app_svc, monitor, settings = await _bootstrap()

            # 启动 monitor(频道白名单在 monitor 起来前先建好,避免漏掉启动期到达的消息)
            t = time.monotonic()
            subscribed = await app_svc.storage.list_subscribed_channels()
            monitor.set_whitelist(c.id for c in subscribed)
            log.info(
                "[setup] loaded %d subscribed channels from storage in %.2fs",
                len(subscribed), time.monotonic() - t,
            )
            t = time.monotonic()
            await monitor.start()
            log.info("[setup] monitor.start() returned in %.2fs", time.monotonic() - t)

            # 启动时自动检测本地 session:有效就直接 ready,无效走 phone_required
            # 这一步会发 LoginStateChanged → main_window 订阅在它之后,所以事件不丢
            state["app"] = app_svc
            state["monitor"] = monitor
            state["settings"] = settings
            t = time.monotonic()
            await app_svc.bootstrap()
            log.info("[setup] app.bootstrap() done in %.2fs", time.monotonic() - t)

            # UI 构造 — 现在 services 都 ready,事件总线已就位
            from tgmonitor.ui.main_window import MainWindow
            win = MainWindow(app_svc, monitor, loop, env_path=env_path)
            # 把 shutdown 协程绑给 window,closeEvent 里同步等待它完成,
            # 然后再让 Qt 进入 quit 流程 — 这样 aiotdlib client.close() / TDLib
            # 内部 thread join 都跑在 CFRunLoop 仍合法的阶段,避开 macOS 的
            # "mutex lock failed: Invalid argument" 析构崩溃。
            win.set_shutdown_callback(_shutdown_async)
            win.show()
            state["win"] = win
            log.info("[setup] full _setup_then_show done in %.2fs",
                     time.monotonic() - t_setup)
        except BaseException as e:  # noqa: BLE001
            # 不能 raise 出 setup_then_show —— 没人在 await 它,异常会被
            # asyncio 吞成 "Task exception was never retrieved"。改成显式记录 + 退出
            setup_failed.append(e)
            log.exception("[setup] failed: %s", e)
            try:
                qt_app.quit()
            except Exception:  # noqa: BLE001
                log.exception("qt_app.quit() raised during setup failure")

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

    # step 1: 调度 setup 到 loop(run_forever 还没跑,Task 等待 loop 启动)
    setup_task = asyncio.ensure_future(_setup_then_show(), loop=loop)

    # 退出钩子:任何路径触发 quit(关窗 / SIGINT)→ **先异步清理** → 再真 quit
    # 这样 step 4 的 async 任务在 loop 仍然 alive 时跑完,避开 'Event loop is closed'。
    def _shutdown_then_quit() -> None:
        # 走到这里说明 aboutToQuit 仍被触发了(非 closeEvent 路径,
        # 比如 macOS 系统菜单 Quit / SIGTERM)。这时只能尽力:
        # 派一个 future,设短超时,失败也不抛 — 不阻塞 Qt quit。
        async def _do_shutdown_then_quit() -> None:
            try:
                await asyncio.wait_for(_shutdown_async(), timeout=5.0)
            except (TimeoutError, Exception):  # noqa: BLE001
                log.exception("best-effort shutdown failed")
            finally:
                qt_app.quit()

        try:
            fut = asyncio.ensure_future(_do_shutdown_then_quit(), loop=loop)
        except RuntimeError:
            # loop 已关(罕见):尽力清理后退出
            log.warning("loop already closed, skipping async shutdown")
            qt_app.quit()
            return

        def _on_done(f: asyncio.Future[None]) -> None:
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

    # 单 loop 持续运行 — setup_task 与 Qt 事件交错 tick,不再有 paused 窗口
    with loop:
        loop.run_forever()
    # 此处 loop 已被 QEventLoop.__exit__ close,async 任务保证在退出前完成
    # 如果 setup 失败,setup_failed 里有异常,告知调用方
    if setup_task.done() and setup_task.exception() is not None:
        # 通常 setup_task.exception() 已被 qt_app.quit 触发而走 cleanup 路径,不会到这里;
        # 这里只是兜底 —— 比如 Qt event loop 在 setup 失败前就退出
        log.warning("setup_task ended with exception: %s", setup_task.exception())

    # 清理 setup_task 异常引用,避免 "Task exception was never retrieved" 警告
    if setup_task.done():
        try:
            setup_task.exception()
        except (asyncio.CancelledError, asyncio.InvalidStateError):
            pass
