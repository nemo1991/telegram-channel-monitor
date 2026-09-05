"""App composition root + UI 启动(qasync 事件循环)。

唯一启动入口 `run()`;装配顺序:
    Settings → EventBus → Storage(connect + init_schema)
                    → ObjectStore(connect)
                    → TelegramClient(TdlibJsonClient or fake)
                    → MonitorService
                    → AppService
                    → UI(QMainWindow)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time

from tgmonitor.core.app_service import AppService
from tgmonitor.core.config import Settings
from tgmonitor.core.events import EventBus
from tgmonitor.core.monitor.service import MediaDownloader, MonitorService
from tgmonitor.core.objectstore.factory import build_object_store
from tgmonitor.core.storage.factory import build_storage
from tgmonitor.core.telegram.factory import build_telegram_client

log = logging.getLogger(__name__)


def _log_level() -> int:
    """日志级别:`TG_LOG_LEVEL` 环境变量(DEBUG/INFO/WARNING/ERROR),默认 INFO。

    排查"一段时间不监听"时设 `TG_LOG_LEVEL=DEBUG`,能看到 monitor 心跳日志。
    """
    name = os.environ.get("TG_LOG_LEVEL", "INFO").upper()
    return getattr(logging, name, logging.INFO)


def _setup_file_logging(level: int) -> None:
    """把日志同时写入数据目录的 `logs/tgmonitor.log`(5MB × 3 轮转,UTF-8)。

    终端 stderr 在 bundle(.app / AppImage 双击启动)里不可见,文件日志是
    排查"心跳正常但收不到消息"等问题的唯一凭据 —— 用户直接把该文件发来即可。
    """
    try:
        from logging.handlers import RotatingFileHandler

        from tgmonitor.core.config import _user_data_dir

        log_dir = _user_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "tgmonitor.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        log.info("file logging enabled: %s", log_dir / "tgmonitor.log")
    except Exception:  # noqa: BLE001
        log.exception("failed to enable file logging")


async def _bootstrap() -> tuple[AppService, MonitorService, Settings, str | None]:
    t0 = time.monotonic()
    settings = Settings()
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
    objects_error: str | None = None
    try:
        await objects.connect()
    except Exception as e:  # noqa: BLE001
        # v1.0.21:对象存储不可用(端点错/凭据错/桶无权限)不阻止应用启动 —
        # 媒体落盘是可降级能力,失败在保存设置时已严格校验;启动这里只降级
        # 记日志,下载任务会标 download_error,用户可感知。
        # v1.0.22:错误信息带回给 UI,主窗口状态栏红字常驻提示(不只写日志)。
        objects_error = str(e)
        log.error(
            "[bootstrap] objectstore.connect() failed, 媒体下载将失败: %s "
            "(backend=%s bucket=%s endpoint=%s)",
            e,
            settings.objectstore_backend.value,
            settings.objectstore_bucket,
            settings.objectstore_endpoint,
        )
    else:
        log.info(
            "[bootstrap] objectstore.connect() took %.2fs backend=%s",
            time.monotonic() - t,
            settings.objectstore_backend.value,
        )

    t = time.monotonic()
    # 凭据未配置时,factory 返回占位 client(UnconfiguredTelegramClient)→ UI
    # 正常启动,显示"未登录"引导,用户在 设置 → 账户 填好凭据重启即可。
    # 真 client 构造失败(如 libtdjson 缺失)仍上抛 → 走 setup 失败弹窗;不
    # 静默回退 fake(历史 bug #22:吞异常返 Fake 导致"无 libtdjson 也能 ready")。
    client = build_telegram_client(settings, use_fake=False, event_bus=bus)
    log.info(
        "[bootstrap] telegram client built in %.2fs kind=%s",
        time.monotonic() - t,
        type(client).__name__,
    )

    # FULL 媒体策略才真正下载原文件:组合根负责接线 MediaDownloader,
    # MonitorService 侧 `downloader=None`(如未接线)时 FULL 策略静默退化为
    # 不下载 — 避免历史 bug:策略选了 FULL 但没有任何下载器在工作。
    monitor = MonitorService(
        bus,
        client,
        storage,
        objects,
        settings,
        downloader=MediaDownloader(
            client,
            storage,
            objects,
            max_bytes=settings.media_max_bytes,
        ),
    )
    # 2026-09-04 v1.6.6:pause 持久化要写 .env,env_path 与 _setup_then_show
    # 末尾传给 MainWindow 的同一份文件(_user_data_dir() / ".env")。
    from tgmonitor.core.config import _user_data_dir

    env_path = _user_data_dir() / ".env"
    app = AppService(
        bus,
        client,
        storage,
        objects,
        settings,
        monitor=monitor,
        # 2026-09-04 v1.6.6:pause 持久化要写 .env,透传 env_path。
        env_path=env_path,
    )
    log.info(
        "[bootstrap] full bootstrap done in %.2fs",
        time.monotonic() - t0,
    )
    return app, monitor, settings, objects_error


def _show_setup_failure_dialog(err: BaseException) -> None:
    """启动失败弹窗 — 替代 stderr 静默退出,让用户知道原因 + 日志位置。

    显示:
      - 异常类型 + 消息
      - 已知常见原因(API_ID/Hash 缺失、.env 不在、platform-native 路径无写权限)
      - 日志路径 = `_user_data_dir()`(用户打开 finder / file manager 看 log)

    设计原则:
      - **不要**主动 import QMessageBox 模块级 — qasync bundle cold-start
        时万一这些 path 出错,弹窗本身就会抛
      - **不要** raise:外面 try 块已经在 `qt_app.quit()` 收尾,绝不能再抛
      - 最多 1 个 dialog,任何内层失败就 log.exception 退化为 just log
    """
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except Exception:  # noqa: BLE001
        log.exception("PySide6 import failed in setup-failure dialog")
        return
    app = QApplication.instance()
    if app is None:
        # Qt 还没初始化到能弹 modal 的状态 — 退化为 just log
        return
    try:
        err_type = type(err).__name__
        err_msg = str(err) or "(no message)"
        box = QMessageBox(
            QMessageBox.Icon.Critical,
            "启动失败",
            f"应用初始化失败:\n\n{err_type}: {err_msg}\n\n"
            "常见原因:\n"
            "  • .env 缺失 / TG_API_ID / TG_API_HASH / TG_PHONE 没填\n"
            "  • TDLib 加密 key 文件损坏(删除 platform-native 目录重试)\n"
            "  • SOCKS5 代理不可达(检查 URL + 网络)\n\n"
            "详细日志请查看下方路径:",
            QMessageBox.StandardButton.Ok,
        )
        # 日志路径 — 跟 README「数据目录」章节一致
        from tgmonitor.core.config import _user_data_dir

        log_dir = _user_data_dir()
        box.setDetailedText(str(log_dir))
        ret = box.exec()
        del ret  # 不用返回值
    except Exception:  # noqa: BLE001
        log.exception("setup-failure dialog raised")


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
    Tasks 处于 paused 状态。tdlib_json 内部 thread 在这段窗口发 IO wakeup 时,
    `Task.__step()` 检查 "loop is the running loop" 失败,抛 `RuntimeError:
    loop ... is not the running loop`,日志刷「qasync._QEventLoop: Exception in
    callback Task.task_wakeup()」。

    新版用单 `run_forever()` + `ensure_future`,loop 始终 running,这窗口不复存在,
    根因消除。

    不要在协程里 `await loop.run_forever()` —— 会撞 "Event loop already running"。
    """
    logging.basicConfig(
        level=_log_level(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _setup_file_logging(_log_level())

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
    state: dict[str, AppService | MonitorService | object] = {}
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
            app_svc, monitor, settings, objects_error = await _bootstrap()

            # 启动 monitor(频道白名单在 monitor 起来前先建好,避免漏掉启动期到达的消息)
            # 2026-09-04 v1.6.6:启动即暂停 — 跳过 monitor.start()(不连 TDLib
            # + 不开 download worker)+ 跳过 app.bootstrap()(不调 client.start())。
            # client.state 保持 "uninit" 是 explicit:用户已选暂停,TDLib 不该连。
            # UI(tray icon / status bar / VM)读 app.is_paused=True → 显 ⏸。
            # 用户点 tray 「恢复监听」走 resume_monitor() 走 client.start() +
            # monitor.start() 正常流程,LoginStateChanged 此时正常 fire。
            t = time.monotonic()
            subscribed = await app_svc.storage.list_subscribed_channels()
            monitor.set_whitelist(c.id for c in subscribed)
            log.info(
                "[setup] loaded %d subscribed channels from storage in %.2fs",
                len(subscribed),
                time.monotonic() - t,
            )

            state["app"] = app_svc
            state["monitor"] = monitor
            state["settings"] = settings

            if app_svc.is_paused:
                log.info(
                    "[setup] settings.paused=true — skip monitor.start() + "
                    "bootstrap() (client stays uninit, UI reads app.is_paused=True → ⏸)"
                )
                state["login_state"] = "ready"
                state["login_detail"] = None
            else:
                t = time.monotonic()
                await monitor.start()
                log.info("[setup] monitor.start() returned in %.2fs", time.monotonic() - t)

                # 启动时自动检测本地 session:有效就直接 ready,无效走 phone_required
                # 这一步会发 LoginStateChanged → main_window 订阅在它之后,所以事件不丢
                t = time.monotonic()
                login_state, login_detail = await app_svc.bootstrap()
                log.info("[setup] app.bootstrap() done in %.2fs", time.monotonic() - t)

            # 启动 orphan reconcile(2026-08-24):dry_run=True 默认只 log 不删,
            # 给 2 秒延迟让 storage / objectstore 完全 ready 再扫。后续 Prune
            # Orphans 按钮(Media Manager 页)走 dry_run=False 显式删。
            async def _startup_reconcile() -> None:
                try:
                    await asyncio.sleep(2.0)
                    await app_svc.reconcile_orphans(dry_run=True)
                except Exception:  # noqa: BLE001 — startup reconcile 不能让 UI 崩溃
                    log.exception("startup orphan reconcile failed")

            asyncio.create_task(_startup_reconcile())

            # UI 构造 — 现在 services 都 ready,事件总线已就位
            from tgmonitor.ui.main_window import MainWindow

            win = MainWindow(app_svc, monitor, loop, env_path=env_path, objects_error=objects_error)
            # 把 shutdown 协程绑给 window,closeEvent 里同步等待它完成,
            # 然后再让 Qt 进入 quit 流程 — 这样 tdlib_json client.close() / TDLib
            # 内部 thread join 都跑在 CFRunLoop 仍合法的阶段,避开 macOS 的
            # "mutex lock failed: Invalid argument" 析构崩溃。
            win.set_shutdown_callback(_shutdown_async)
            win.show()
            state["win"] = win
            log.info("[setup] full _setup_then_show done in %.2fs", time.monotonic() - t_setup)
        except BaseException as e:  # noqa: BLE001
            # 不能 raise 出 setup_then_show —— 没人在 await 它,异常会被
            # asyncio 吞成 "Task exception was never retrieved"。改成显式记录 + 退出
            setup_failed.append(e)
            log.exception("[setup] failed: %s", e)
            # 启动失败:Qt 弹窗告诉用户原因 + 日志位置 + bundle 内置 log path;
            # 否则 .app / .AppImage 双击启动写 stderr 用户看不到,会以为"点了没反应"。
            # 弹窗在 qt_app.quit() 之前调,避免 quit 抢关窗口让 dialog 闪现消失。
            _show_setup_failure_dialog(e)
            try:
                qt_app.quit()
            except Exception:  # noqa: BLE001
                log.exception("qt_app.quit() raised during setup failure")

    async def _shutdown_async() -> None:
        monitor = state.get("monitor")
        app_svc = state.get("app")
        if isinstance(monitor, MonitorService):
            try:
                await monitor.stop()
            except Exception:  # noqa: BLE001
                log.exception("monitor.stop() failed")
        if isinstance(app_svc, AppService):
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

    # 2026-08-30 v1.5.0 PR #A4:关闭最后一个窗口时不退出(Qt 默认行为)
    # — 关窗 → minimize 到 tray(由 MainWindow.closeEvent 拦截);真退出走
    # File→Quit / tray「退出」→ `_quit_app` → `qt_app.quit()` →
    # `aboutToQuit` → `_shutdown_then_quit` → 干净退出。
    # PySide6 6.11 .pyi 缺 `setQuitOnLastWindowClosed`(运行期 QApplication /
    # QGuiApplication 都有该方法)— type: ignore[attr-defined]。
    qt_app.setQuitOnLastWindowClosed(False)  # type: ignore[attr-defined]

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
