"""App composition root + UI 启动(qasync 事件循环)。

唯一启动入口 `run()`;实际装配在 `core.runtime.bootstrap()`(CLI / GUI 共用),
本模块负责 Qt / qasync 事件循环 + Window.show()。

2026-09-27 refactor:`_bootstrap` 与 `_shutdown_async` 抽到 `core.runtime`
后,本模块顶层**不**import PySide6 / qasync / MainWindow — CLI 模式
(`tgmonitor sync` / `tgmonitor monitor`)调用 `__main__.py` 走 `cli.main`,
完全不加载本模块,验证见 `tests/test_cli.py::test_cli_does_not_import_qt_or_qasync`。

GUI 启动走 `run()`:lazy import `MainWindow`(函数体内),事件循环走 qasync。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from typing import cast

from tgmonitor.core.app_service import AppService
from tgmonitor.core.config import Settings
from tgmonitor.core.events import ErrorOccurred
from tgmonitor.core.monitor.service import MonitorService

# 2026-09-27 refactor:`_bootstrap` 与 `_shutdown_async` 抽到
# `tgmonitor.core.runtime`,CLI / GUI 共用同一份 composition root。
from tgmonitor.core.runtime import bootstrap as _bootstrap
from tgmonitor.core.runtime import shutdown as _runtime_shutdown

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

    # 2026-09-07 v1.6.8:接 install_translator —— 之前 v1.5.3 PR #D3 定义
    # 了完整逻辑但全工程没人调,tr() 形同虚设。这里同步构造 Settings 读
    # `lang` 字段(env TG_LANG,默认 zh_CN),然后装翻译;`_bootstrap` 后面会
    # 再读一次 Settings(同一份 .env,幂等)。
    from tgmonitor.i18n import install_translator

    early_settings = Settings()
    install_translator(qt_app, locale=early_settings.lang)
    log.info("[i18n] locale=%s 已装翻译器", early_settings.lang)

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

    # 2026-09-27 refactor:`MainWindow` 改在函数体内 lazy import —
    # 之前在模块顶,CLI 模式(`python -m tgmonitor sync`)会被迫加载 PySide6。
    # 现在 `__main__.py:main()` 在子命令分支直接调 `cli.main` 不进 `run()`,
    # PySide6 永不加载(验证见 `tests/test_cli.py::test_cli_does_not_import_qt_or_qasync`)。
    from tgmonitor.ui.main_window import MainWindow

    async def _setup_then_show() -> None:
        """一次性做完:async 装配 → MainWindow 构造 → window.show() → 后台启动服务。

        2026-09-25 v1.8.x:把 list_subscribed + set_whitelist + monitor.start +
        app.bootstrap 拆到 `_background_startup` 后台 task,让 win.show() 立即
        执行 — 避免冷启动 / libtdjson 慢(1-5s,断网可达 10s+)时用户对着空白
        dock 图标干等。UI 在 win.show() 后立即可见,后台启动各 step 通过
        `win.status_bar.show_activity(...)` 在状态栏左侧活动指示器滚动显示。

        整个跑在 qasync 的 loop 上,与 Qt 事件交错。这样 loop 始终 running,
        彻底去掉旧 `run_until_complete` + `run_forever` 中间的 paused 窗口。
        """
        try:
            t_setup = time.monotonic()
            app_svc, monitor, settings, objects_error = await _bootstrap(
                settings=early_settings,
                env_path=env_path,
            )

            state["app"] = app_svc
            state["monitor"] = monitor
            state["settings"] = settings

            # UI 构造 — 现在 services 已构造,事件总线已就位。
            # 注意:此时 monitor 还没 start() / bootstrap 还没跑,但 UI 读
            # app.is_paused / monitor.subscribed_ids 都有兜底,header 状态由
            # LoginStateChanged 驱动(后台 bootstrap 完成后会自动 fire)。
            win = MainWindow(app_svc, monitor, loop, env_path=env_path, objects_error=objects_error)
            # 把 shutdown 协程绑给 window,closeEvent 里同步等待它完成,
            # 然后再让 Qt 进入 quit 流程 — 这样 tdlib_json client.close() / TDLib
            # 内部 thread join 都跑在 CFRunLoop 仍合法的阶段,避开 macOS 的
            # "mutex lock failed: Invalid argument" 析构崩溃。
            win.set_shutdown_callback(_shutdown_async)
            win.show()
            state["win"] = win
            log.info("[setup] win.show() done in %.2fs", time.monotonic() - t_setup)

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

            # 后台启动服务(白名单 + monitor + bootstrap)— win 可见之后异步跑,
            # 不阻塞主窗口出现。activity label 滚动显示当前步骤。
            asyncio.create_task(_background_startup(app_svc, monitor, win))
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

    async def _background_startup(
        app_svc: AppService,
        monitor: MonitorService,
        win: MainWindow,
    ) -> None:
        """win.show() 之后在后台跑的启动步骤:

        1. 加载白名单(list_subscribed + set_whitelist)
        2. monitor.start()(起 asyncio worker)
        3. app.bootstrap()(调 libtdjson 启动会话,触发 LoginStateChanged)

        每步通过 `win.status_bar.show_activity(...)` 更新状态栏左侧活动指示器,出错走
        ErrorOccurred 事件(bus subscriber 已存在,自动弹窗/计数)。

        2026-09-04 v1.6.6:启动即暂停 — 跳过 monitor.start + bootstrap。
        client.state 保持 "uninit",用户点 tray「继续监听」走 resume_monitor。
        """
        try:
            # Step 1: 加载白名单
            win.status_bar.show_activity("加载已订阅频道...")
            t = time.monotonic()
            subscribed = await app_svc.storage.list_subscribed_channels()
            monitor.set_whitelist(c.id for c in subscribed)
            log.info(
                "[startup-bg] loaded %d subscribed channels from storage in %.2fs",
                len(subscribed),
                time.monotonic() - t,
            )

            if app_svc.is_paused:
                log.info(
                    "[startup-bg] settings.paused=true — skip monitor.start() + "
                    "bootstrap() (client stays uninit, UI reads app.is_paused=True → ⏸)"
                )
                win.status_bar.show_activity("监听已暂停 — 点 tray「继续监听」启动")
                return

            # Step 2: 启动 monitor
            win.status_bar.show_activity("启动监听服务...")
            t = time.monotonic()
            await monitor.start()
            log.info("[startup-bg] monitor.start() returned in %.2fs", time.monotonic() - t)

            # Step 3: bootstrap libtdjson — 触发 LoginStateChanged,header 自动跟进
            win.status_bar.show_activity("连接 Telegram...")
            t = time.monotonic()
            login_state, login_detail = await app_svc.bootstrap()
            log.info(
                "[startup-bg] app.bootstrap() done in %.2fs state=%s",
                time.monotonic() - t,
                login_state,
            )

            # 稳态:清空活动指示器(后续 LoginStateChanged / 错误事件会再次填充)
            win.status_bar.show_activity("")
        except Exception as exc:
            log.exception("[startup-bg] failed")
            try:
                win.status_bar.show_activity(f"启动失败: {exc}")
            except Exception:  # noqa: BLE001
                pass
            await app_svc.bus.publish(
                ErrorOccurred(source="startup", message=str(exc), exception=exc)
            )

    # 2026-09-27 refactor:实际关停走 `core.runtime.shutdown`(GUI / CLI 共用)。
    # 此处只保留内层闭包 — 读 `state["app"]` / `state["monitor"]`(由
    # `_setup_then_show` 在装配完成后填入),传给 `_runtime_shutdown`。
    async def _shutdown_async() -> None:
        # state 是 dict[str, ...] 联合容器,mypy 无法自动 narrow;
        # 用 cast 显式收紧类型,让 `_runtime_shutdown` 类型契约成立
        app_svc = cast(AppService | None, state.get("app"))
        monitor_svc = cast(MonitorService | None, state.get("monitor"))
        if app_svc is None or monitor_svc is None:
            log.warning("shutdown skipped: app/monitor not initialized")
            return
        await _runtime_shutdown(app_svc, monitor_svc)

    # step 1: 调度 setup 到 loop(run_forever 还没跑,Task 等待 loop 启动)
    setup_task = asyncio.ensure_future(_setup_then_show(), loop=loop)

    # 退出钩子:任何路径触发 quit(关窗 / SIGINT)→ **先异步清理** → 再真 quit
    # 这样 step 4 的 async 任务在 loop 仍然 alive 时跑完,避开 'Event loop is closed'。
    def _shutdown_then_quit() -> None:
        # 2026-09-22 v1.8.x:之前这里 `asyncio.ensure_future` 调度后立刻返回,
        # Qt 继续 quit → loop close → future 还没跑就被 cancel → TDLib 子进程
        # 未 join / storage 连接未关。改为嵌套 subloop 同步等(与
        # `MainWindow.closeEvent` 同模式),`_shutdown_async` 真跑完才让
        # `qt_app.quit()` 返回、loop close。
        #
        # 该路径覆盖:macOS dock Cmd+Q / SIGINT / SIGTERM — 这些没走
        # `MainWindow.closeEvent`,只能指望 aboutToQuit;现在也变可靠。
        # `_shutdown_async` 内每个 stage 自带 timeout(client 2s / monitor 2s,
        # 见 `AppService.shutdown` + `_shutdown_async`),最坏 ~5s,hard
        # upper bound 留 8s 缓冲。
        from tgmonitor.ui.main_window import run_shutdown_coro_sync

        run_shutdown_coro_sync(loop, _shutdown_async, deadline_ms=8_000)

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
