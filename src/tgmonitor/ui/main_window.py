"""主窗口 — 导航(左) + 内容页(QStackedWidget)。

2026-11-19 v2.0.8:新增消息管理页 —— 内容页 3→4。

  ┌─────────────────────────────────────────────┐
  │ ●                          已登录           │ ← 紧凑头栏(纯标题)
  ├──┬──────────────────────────────────────────┤
  │  │  QStackedWidget                           │
  │  │   0: 大盘(DASHBOARD) — 统计 + 活动 + 实时流 │
  │  │   1: 消息管理(MESSAGES) — MessageManagerWidget │
  │  │   2: 媒体管理(MEDIA) — MediaManagerWidget  │
  │  │   3: 设置(SETTINGS) — 整页配置             │
  │  │      └ 监听频道管理 — ChannelWidget        │
  ├──┴──────────────────────────────────────────┤
  │ Ready · 3 channels · 0 new                  │ ← 状态栏
  └─────────────────────────────────────────────┘

退出路径(保持与旧版一致):
  closeEvent → 同步阻塞 async shutdown → accept
  aboutToQuit → 尽力清理(备用)
"""

# mypy: disable-error-code="attr-defined"
# PySide6 6.6+.pyi 漏了一堆 class-level const(QFrame.NoFrame / Qt.UserRole /
# QHeaderView.ResizeToContents / QDialogButtonBox.Ok/AcceptRole / ...)— 全
# ui 文件统一关掉 attr-defined,留下 union-attr / var-annotated 等真错要清。
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, cast

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QStackedWidget,
    QWidget,
)

from tgmonitor.core.events import (
    LoginStateChanged,
    NotificationRequested,
)
from tgmonitor.core.settings_store import read_close_on_exit
from tgmonitor.ui.controllers import (
    AppStateController,
    ExportController,
    LifecycleController,
    MainWindowCtx,
    MediaActionsController,
    MessageStreamController,
    SettingsReactionController,
    SyncController,
    TrayController,
)
from tgmonitor.ui.nav_bar import VerticalNavBar
from tgmonitor.ui.shutdown import run_shutdown_coro_sync
from tgmonitor.ui.ui_assembler import UiAssembler
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel
from tgmonitor.ui.widgets.channel_widget import ChannelWidget
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget
from tgmonitor.ui.widgets.media_manager_widget import MediaManagerWidget
from tgmonitor.ui.widgets.message_detail import MessageDetail
from tgmonitor.ui.widgets.message_manager_widget import MessageManagerWidget
from tgmonitor.ui.widgets.message_view import MessageView
from tgmonitor.ui.widgets.settings_page import SettingsPage
from tgmonitor.ui.widgets.status_bar import StatusBar

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)

ShutdownCb = Callable[[], Awaitable[None]]

# ---- 状态映射 ----
# 2026-11-16 v2.0.2:LIVE 多选 / 右键菜单整体移除,原 items 归一化逻辑随之删除。


class MainWindow(QMainWindow):
    """应用主窗口:左导航 + 4 页内容 + 紧凑头栏 + 状态栏。

    # 4 个 page 由 QStackedWidget 持有(v2.0.8):
    #   0 DASHBOARD  → 统计 + 活动时间线 + 实时流(MessageView + Detail)
    #   1 MESSAGES   → MessageManagerWidget(历史消息查看 / 筛选)
    #   2 MEDIA      → MediaManagerWidget(浏览 / 重试 / 删 / 打开 + prune)
    #   3 SETTINGS   → 整页配置(凭据 / 监听频道 / 存储 / 代理 / 媒体 / 同步)
    #
    # 退出:closeEvent 同步阻塞等 async shutdown 完成,保证 storage / client /
    #       tdjson 子进程都被显式关掉。
    """

    def __init__(
        self,
        app: AppService,
        monitor: MonitorService,
        loop: asyncio.AbstractEventLoop,
        env_path: Path | None = None,
        objects_error: str | None = None,
    ) -> None:
        """构造主窗口 + 装配子 widget + 连信号 + 触发初始刷新。

        `env_path` fallback 跟 `app.py` 同步:platform-native
        (`~/.local/share/tgmonitor/.env` 或 `~/Library/Application Support/tgmonitor/.env`),
        不依赖 cwd。

        `objects_error` = 启动 bootstrap 时对象存储 connect 失败的原因
        (v1.0.22 起由 app.py 传入);非空则在状态栏红字常驻提示,用户可在
        设置页改好配置后热重载清除。
        """
        super().__init__()
        # v1.0.1:env_path fallback 跟 app.py 同步 — platform-native
        # (~/.local/share/tgmonitor/.env / ~/Library/Application Support/tgmonitor/.env),
        # 不依赖 cwd。
        from tgmonitor.core.config import _user_data_dir

        resolved_env_path: Path = env_path or (_user_data_dir() / ".env")
        self._setup_core(app, monitor, loop, resolved_env_path, objects_error)
        # 2026-10-12 plan-batch3-1.2:UiAssembler 装配 central widget +
        # 5 page + status bar + debounce。原 `MainWindow._build_central_widget`
        # 115 LOC body 整体搬到 `UiAssembler.build_central`,这里只做 widget
        # dict → self 属性赋值 + setCentralWidget。
        self._assembler = UiAssembler(self)
        self._build_ui()
        # 2026-10-12 plan-batch3-1.3:_build_menu 31 LOC + _build_tray 15 LOC
        # 抽到 UiAssembler。act_show / act_quit 仍是 self. 属性。
        menu = self._assembler.build_menu()
        self.act_show = cast(QAction, menu["act_show"])
        self.act_quit = cast(QAction, menu["act_quit"])
        self._tray = self._assembler.build_tray()
        # 2026-10-12 plan-batch3-1.5:_wire_events 抽到 UiAssembler.wire_events;
        # MainWindow 编排只剩 1 行调。
        self._assembler.wire_events()
        self._refresh_state()
        self._vm.bootstrap_ui()
        self._setup_controllers()

    def _setup_core(
        self,
        app: AppService,
        monitor: MonitorService,
        loop: asyncio.AbstractEventLoop,
        env_path: Path,
        objects_error: str | None,
    ) -> None:
        """2026-10-04 plan 阶段 3.2:`__init__` 拆 3 段 — 核心字段 + VM + tray + 4 VM 信号。

        装配顺序故意保持原 __init__ 的执行序列(flag 必须在 super 之后立即置位,
        env_path fallback 必须在 setWindowTitle 之前,VM 信号必须在 controller
        装配之前就位以确保 controller 调 main_window.*_vm 时 handler 已 connect)。
        """

        self.app = app
        self.monitor = monitor
        self.loop = loop
        self._objects_error = objects_error
        # 2026-08-30 v1.5.0 PR #A4:tray 图标「真退出」标志。False 时
        # closeEvent 触发即最小化到托盘 + 状态栏提示,真退出走 File→Quit
        # 菜单或 tray「退出」菜单项(都 → `qt_app.quit()` → `aboutToQuit` →
        # `_shutdown_then_quit` 路径)。一旦置 True,后续 closeEvent 直走
        # shutdown(防循环)。
        self._truly_quit = False
        # 2026-08-30 PR #A4:用户已通过 tray menu 看到首次关闭提示,
        # 后续关窗不再弹通知,只在 tray 静默 hide。
        self._tray_first_close_hint_shown = False
        # v1.0.1:env_path fallback 跟 app.py 同步 — platform-native
        # (~/.local/share/tgmonitor/.env / ~/Library/Application Support/tgmonitor/.env),
        # 不依赖 cwd。
        from tgmonitor.core.config import _user_data_dir

        self.env_path = env_path or (_user_data_dir() / ".env")  # noqa: F841 — _user_data_dir already imported
        self.setWindowTitle(self.tr("tgmonitor · Telegram 频道监听"))
        self.resize(1180, 740)

        self._vm = MonitorViewModel(app, monitor, loop)
        self._shutdown_cb: ShutdownCb | None = None
        # 2026-08-30 v1.5.0 PR #A4:TrayIcon 实例,line 157 `_assembler.build_tray()`
        # 装配(`TrayIcon` 类型,永非 None)。`is_active=False` 时(offscreen /
        # Linux 无 indicator)closeEvent 不进入 minimize-to-tray。
        # 2026-08-30 PR #A4:tray「退出」→ VM 收到 QuitRequested → emit
        # `quit_requested` signal → 此 handler → 真退出 qt_app.quit。
        self._vm.quit_requested.connect(self._on_vm_quit_requested)
        # 2026-09-03 v1.6.1:暂停 / 恢复信号 — status bar label + window title 后缀
        self._vm.monitoring_paused.connect(self._on_monitoring_paused)
        self._vm.monitoring_resumed.connect(self._on_monitoring_resumed)

    def _setup_controllers(self) -> None:
        """装配 MainWindowCtx + controller。

        controller 间无顺序依赖(各自只读 ctx 字段)。2026-11-16 v2.0.2
        LIVE 只读化后移除 SearchController / LiveSelectionController /
        LiveActionsController,搜索 debounce timer 与相关 signal wire 一并删除。
        """

        # 2026-11-16 v2.0.2:LIVE 页只读化 — 移除搜索 debounce timer、
        # LiveActionsController(右键菜单 + 选区广播)装配。
        # QTimer 仍用于 media_refresh_debounce(见 UiAssembler.build_central)。

        # ======================== controller 装配 ========================
        # 复杂 2 个(media_actions / message_stream)留 main_window.py 自身。
        # 2026-10-04 plan 阶段 3.4:反向调用通道注入 `window_title_setter` /
        # `quit_app` lambda,controller 不再持 `main_window` 反向调出去。
        self._ctx = MainWindowCtx(
            app=self.app,
            monitor=self.monitor,
            loop=self.loop,
            vm=self._vm,
            env_path=self.env_path,
            main_window=self,
            window_title_setter=self.setWindowTitle,
            quit_app=self._quit_app,
            live_view=self.live_view,
            message_detail=self.message_detail,
            media_manager=self.media_manager,
            dashboard=self.dashboard,
            channel_panel=self.channel_panel,
            status_bar=self.status_bar,
            nav=self.nav,
            stack=self.stack,
            tray=self._tray,
            media_refresh_debounce=self._media_refresh_debounce,
            settings_page=self.settings_page,
            message_manager=self.message_manager,
        )
        self._tray_ctrl = TrayController(self._ctx)
        # 2026-11-17 v2.0.3 D3:登录/登出入口迁到 SettingsPage 自管,
        # HeaderActionController 随纯标题栏一起移除。
        self._settings_reaction_ctrl = SettingsReactionController(self._ctx)
        self._export_ctrl = ExportController(self._ctx)
        self._sync_ctrl = SyncController(self._ctx)
        # 2026-11-19:全量同步入口（SyncFlowController + sync_dialog）整体移除；
        # SyncController 保留用于进度/完成事件反馈与已监听列表刷新。
        # 2026-10-09 plan 目标 1.2:抽 `AppStateController` — 接管 C+D 类 82 LOC
        # (VM login/conn/error + EventBus notification/login + bell/clear)。
        self._app_state_ctrl = AppStateController(self._ctx)
        # 2026-10-10 plan 目标 1.4:抽 `LifecycleController` — 接管 F 类 110 LOC
        # (show_and_raise / quit_app / switch_tab / on_nav_changed /
        # focus_search / on_global_escape / copy_current_message_text)。
        self._lifecycle_ctrl = LifecycleController(self._ctx)
        # 2026-11-16 v2.0.2:LIVE 页只读化 — 移除 SearchController、
        # LiveSelectionController(批量动作 + 右键 + 详情导出/删除)及其全部接线。
        # 详情仅保留「复制文本」toast —— 内联到 MainWindow 本地槽。

        # 2026-10-04 plan 阶段 3.3:media_actions controller — 抽 11 MediaManagerWidget
        # signal + 2 helper(clear_channel dialog / lightbox decoder)+ 4 VM media event
        # + 1 debounce timer slot。`_qloop` bug(原 `_on_media_reveal/copy` 用未赋值的
        # `self._qloop`,运行到会 AttributeError)统一改 `ctx.loop`。
        self._media_actions_ctrl = MediaActionsController(self._ctx)
        # ---- MediaManagerWidget signal wire(controller 实例化后立即连) ----
        self.media_manager.refresh_requested.connect(self._media_actions_ctrl.on_media_refresh)
        self.media_manager.open_requested.connect(
            lambda cid, mid, idx: self._vm.open_media(cid, mid, idx)
        )
        # 2026-08-27 v1.4.0 PR #16:Reveal / Copy 按钮 wire。
        self.media_manager.reveal_requested.connect(self._media_actions_ctrl.on_media_reveal)
        self.media_manager.copy_requested.connect(self._media_actions_ctrl.on_media_copy)
        # 2026-11-22 v2.0.9 FR-06:未下载媒体手动下载(force=False),与 retry 分义。
        self.media_manager.download_requested.connect(
            self._media_actions_ctrl.on_media_download_one
        )
        self.media_manager.retry_requested.connect(
            lambda cid, mid, idx: self._vm.retry_media(cid, mid, idx)
        )
        self.media_manager.delete_requested.connect(
            lambda cid, mid, idx: self._vm.delete_media(cid, mid, idx)
        )
        self.media_manager.batch_retry_requested.connect(
            self._media_actions_ctrl.on_media_batch_retry
        )
        self.media_manager.batch_delete_requested.connect(
            self._media_actions_ctrl.on_media_batch_delete
        )
        # 2026-11-22 用户选型:批量下载未下载媒体(所选 / 当前筛选全部)
        self.media_manager.batch_download_requested.connect(
            self._media_actions_ctrl.on_media_batch_download
        )
        self.media_manager.download_all_pending_requested.connect(
            self._media_actions_ctrl.on_media_download_all_pending
        )
        self.media_manager.prune_requested.connect(self._media_actions_ctrl.on_media_prune)
        # 2026-08-25 PR #4:按频道批量删除 — 二次确认后调 vm.delete_by_channel
        self.media_manager.clear_channel_requested.connect(
            self._media_actions_ctrl.on_media_clear_channel
        )
        # 2026-08-25 v1.3.0 PR #7:Media Manager 当前视图 → CSV 一键导出
        self.media_manager.export_csv_requested.connect(
            self._media_actions_ctrl.on_media_export_csv
        )
        # 2026-09-01 v1.5.1 PR #B4:ZIP 打包导出 — 镜像 CSV 流程,接收
        # `(out_path, include_thumbnails)`,构造 ExportRequest(format=ZIP)。
        self.media_manager.export_zip_requested.connect(
            self._media_actions_ctrl.on_media_export_zip
        )
        # 2026-08-31 v1.5.0 PR #A8:Lightbox 内嵌预览 — 点缩略图 → 异步加载
        # 原图 bytes → QPixmap → 弹 LightboxDialog。
        self.media_manager.preview_requested.connect(self._media_actions_ctrl.on_media_preview)
        # 2026-08-25 v1.3.0 PR #5:打开媒体失败 → QMessageBox.warning 带原因
        self._vm.open_media_failed.connect(self._media_actions_ctrl.on_open_media_failed)
        # MessageDetail 媒体卡 click → Lightbox(与 media_manager.preview_requested
        # 走同一个加载器)
        self.message_detail.preview_requested.connect(self._media_actions_ctrl.on_media_preview)
        # 2026-11-16 v2.0.2:详情「复制文本」toast — 实际 clipboard 写入在
        # MessageDetail._on_copy_text 内完成,这里只做状态栏提示。
        self.message_detail.copy_text_requested.connect(self._on_detail_copy_text)
        # ---- VM media 事件回调(下载结束 / 重试 / 删除 + debounce timer) ----
        self._vm.media_downloaded.connect(self._media_actions_ctrl.on_media_downloaded)
        self._vm.media_retried.connect(self._media_actions_ctrl.on_media_retried)
        self._vm.media_deleted.connect(self._media_actions_ctrl.on_media_deleted)
        self._media_refresh_debounce.timeout.connect(
            self._media_actions_ctrl.flush_media_refresh_pending
        )

        # 2026-10-04 plan 阶段 3.3:message_stream controller — 抽 VM
        # `message_received` / `message_edited` / `message_interactions_changed`
        # + EventBus `MessageDeleted` 4 handler 到独立 plain class。
        self._message_stream_ctrl = MessageStreamController(self._ctx)
        # ---- VM LIVE 流消息 signal wire ----
        self._vm.message_received.connect(self._message_stream_ctrl.on_message_received)
        self._vm.message_edited.connect(self._message_stream_ctrl.on_message_edited)
        self._vm.message_interactions_changed.connect(
            self._message_stream_ctrl.on_message_interactions_changed
        )
        # ---- EventBus MessageDeleted 订阅(LIVE 流删行) ----
        from tgmonitor.core.events import MessageDeleted  # noqa: PLC0415

        self.app.bus.subscribe(MessageDeleted, self._message_stream_ctrl.on_bus_message_deleted)
        # 2026-10-09 v1.12.x:`AppStateController` 5 个 wire(原 `_wire_events` /
        # `_build_tray` 早于 `_setup_controllers`,挪到此处确保 controller
        # 已实例化):
        #   - VM login_state / conn_state / error
        #   - EventBus LoginStateChanged / NotificationRequested
        #   - status_bar._bell.bell_clicked
        self._vm.login_state.connect(self._app_state_ctrl.on_login_state)
        self._vm.conn_state.connect(self._app_state_ctrl.on_conn_state)
        self._vm.error.connect(self._app_state_ctrl.on_error)
        self.app.bus.subscribe(LoginStateChanged, self._app_state_ctrl.on_bus_login)
        self.status_bar._bell.bell_clicked.connect(self._app_state_ctrl.on_bell_clicked)
        self.app.bus.subscribe(NotificationRequested, self._app_state_ctrl.on_notification_fallback)

    def set_shutdown_callback(self, cb: ShutdownCb) -> None:
        """由 `app.py` 在主循环开始时注入 — closeEvent 触发时调起。"""
        self._shutdown_cb = cb

    def _close_on_exit(self) -> bool:
        """设置页「关闭窗口时退出程序」— 默认 True。单独抽方法便于测试替换。

        从 .env 实时读(而非 app.settings):只改这一项时 core 的 reconfigure
        diff 判无变化会提前 return,内存 Settings 仍是旧值。
        """
        return read_close_on_exit(getattr(self, "env_path", None))

    # ======================== closeEvent ========================

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """窗口关闭 — 默认真退出;关掉设置后隐藏到托盘。

        设置页「关闭窗口时退出程序」开启(默认)时走 `qt_app.quit()` →
        `aboutToQuit` 统一清理;关闭且托盘可用时隐藏到托盘。只有托盘不可用
        或 `_truly_quit=True` 的兜底路径才在本方法里同步等 async shutdown。

        2026-09-23 v1.8.x:`run_shutdown_coro_sync` helper 不嵌套 QEventLoop
        exec — macOS 26.x arm64 CI 镜像(`macos-26-arm64` 20260728)嵌套 exec
        偶发 segfault,Windows 真机平台插件更稳定触发。

        qasync 的 loop 就跑在 Qt 主线程上(与 closeEvent 同线程),asyncio 回调
        由 Qt timer / queued signal 驱动。helper 在等 future 的同时用
        `QApplication.processEvents()` 泵 Qt 事件让 shutdown 协程推进 —— 既不死等
        阻塞 loop(那会必然超时),也不嵌套 exec。详见 `tgmonitor.ui.shutdown`。

        任何意外(`RuntimeError` / `BaseException` 含 `CancelledError`)由
        最外层 try/except 兜底 — Qt 的 `closeEvent` 不应让 Python 异常抛回
        主循环,否则会变 "Error calling Python override" 把窗口关成 fail。
        """
        # 2026-11-24:设置页「关闭窗口时退出程序」默认开启 — 点 X / Alt+F4
        # 走与 File→Quit / tray「退出」相同的 `qt_app.quit()` → `aboutToQuit`
        # → `_shutdown_then_quit` 单一清理路径。这里**不**再跑下方的同步
        # shutdown,避免 aboutToQuit 重复清理。
        # 关掉该设置才恢复 v1.5.0 起的「首次 close 隐藏到 tray」行为。
        if not self._truly_quit:
            if self._close_on_exit():
                log.info("closeEvent: close_on_exit enabled → qt_app.quit()")
                self._truly_quit = True
                qt_app = QApplication.instance()
                if qt_app is not None:
                    qt_app.quit()
                super().closeEvent(event)
                return
            if self._tray is not None and self._tray.is_active:
                self.hide()
                if not self._tray_first_close_hint_shown:
                    self._tray_first_close_hint_shown = True
                    # 系统通知(若有 tray)+ 状态栏永久提示
                    self.app.bus.publish_threadsafe(
                        self.loop,
                        NotificationRequested(
                            level="info",
                            title=self.tr("tgmonitor 已在后台运行"),
                            body=self.tr("右键托盘图标可恢复窗口或退出应用"),
                            click_action="show_main",
                        ),
                    )
                    self.status_bar.show_message(
                        self.tr("已在后台运行 · 右键托盘图标或 File 菜单恢复"),
                        8000,
                    )
                event.ignore()
                return
            # 2026-09-22 v1.8.x:tray 不可用(offscreen / Linux server /
            # 老 Windows 无 indicator)时 `is_active=False`,之前这条 branch
            # 跳过 → closeEvent 落到下方 `_shutdown_cb` 路径 → 但
            # `setQuitOnLastWindowClosed(False)` 让 Qt 不退出 → 进程进入
            # 「窗口全 hide 但 loop 仍跑」无界面状态,只能任务管理器杀。
            # 改为 tray 不可用时直接 `_truly_quit=True` 走 shutdown。
            log.info("closeEvent: tray inactive, falling through to _truly_quit=True → shutdown")
            self._truly_quit = True
        if self._shutdown_cb is not None:
            try:
                # 2026-09-22 v1.8.x:嵌套 subloop 同步等模式抽出到
                # `run_shutdown_coro_sync`(tgmonitor.ui.shutdown module),这里直接复用。
                # 测试可通过 `_close_deadline_ms` 缩 deadline,避免 sleep(15)
                # 让 CI 浪费 30s。
                run_shutdown_coro_sync(
                    self.loop,
                    self._shutdown_cb,
                    deadline_ms=getattr(self, "_close_deadline_ms", 10_000),
                )
            except BaseException:  # noqa: BLE001
                # 最后一道闸:任何意外(包括 CancelledError)都不应让
                # Qt closeEvent 抛回主循环导致 "Error calling Python override"。
                log.exception("closeEvent: unexpected error in shutdown")
        super().closeEvent(event)

    # ======================== UI 装配 ========================

    def _build_ui(self) -> None:
        """2026-10-04 plan 阶段 3.1:拆 `_build_central_widget` + `_wire_signals` 两段。

        本方法只做编排,装配细节见两个私有方法。

        2026-10-12 plan-batch3-1.2:`_build_central_widget` 抽到 `UiAssembler.build_central`,
        本方法只做 widget dict → self 属性赋值 + setCentralWidget + 调 `_wire_signals`。
        """
        widgets = self._assembler.build_central()
        # UiAssembler.build_central() 返 `dict[str, object]`(整体 schema 边界),
        # 每个 widget 实际类型在 assembler 内部已 typing 完整。MainWindow 端 cast
        # 一次即可,不必引入 TypedDict(plan-batch3-1.2 决策)。
        self.nav = cast(VerticalNavBar, widgets["nav"])
        self.stack = cast(QStackedWidget, widgets["stack"])
        self.live_view = cast(MessageView, widgets["live_view"])
        self.message_detail = cast(MessageDetail, widgets["message_detail"])
        self.dashboard = cast(DashboardWidget, widgets["dashboard"])
        self.channel_panel = cast(ChannelWidget, widgets["channel_panel"])
        self.media_manager = cast(MediaManagerWidget, widgets["media_manager"])
        self.message_manager = cast(MessageManagerWidget, widgets["message_manager"])
        self.settings_page = cast(SettingsPage, widgets["settings_page"])
        self.status_bar = cast(StatusBar, widgets["status_bar"])
        self._media_refresh_debounce = cast(QTimer, widgets["media_refresh_debounce"])
        self.setCentralWidget(cast(QWidget, widgets["central"]))
        # 2026-10-12 plan-batch3-1.4:_wire_signals 41 LOC 抽到 UiAssembler。
        self._assembler.wire_signals()

    def _show_and_raise(self) -> None:
        """2026-10-10 v1.12.x:thin delegate `LifecycleController.show_and_raise`。
        保留方法名以保持测试夹具引用稳定(`test_main_window_tray.py`)。
        """
        self._lifecycle_ctrl.show_and_raise()

    def _quit_app(self) -> None:
        """2026-10-10 v1.12.x:thin delegate `LifecycleController.quit_app`。
        保留方法名以保持测试夹具引用稳定(`test_main_window_tray.py`)。
        """
        self._lifecycle_ctrl.quit_app()

    def _on_vm_quit_requested(self) -> None:
        """2026-08-30 v1.5.0 PR #A4:tray menu「退出」→ VM 转发 → 真退出。

        与 `_quit_app` 同义,但走 tray「退出」(不绕开 Qt 主循环)路径:
        VM `quit_requested` signal → 此 slot → `qt_app.quit`。
        2026-10-01 v1.12.x:委托 `TrayController`。
        """
        self._tray_ctrl.on_vm_quit_requested()

    def _on_monitoring_paused(self, source: str) -> None:
        """2026-09-03 v1.6.1:监听已暂停 — 状态栏常驻 label 显示 +
        window title 加 `(⏸ 暂停)` 后缀。委托 `TrayController`(v1.12.x)。
        """
        self._tray_ctrl.on_monitoring_paused(source)

    def _on_monitoring_resumed(self, source: str) -> None:
        """2026-09-03 v1.6.1:监听已恢复 — 状态栏 label 隐藏 + title 复位。
        委托 `TrayController`(v1.12.x)。
        """
        self._tray_ctrl.on_monitoring_resumed(source)

    async def _on_notification_fallback(self, event: object) -> None:
        """2026-10-09 v1.12.x:thin delegate `AppStateController.on_notification_fallback`。

        保留方法名以保持测试夹具引用稳定(实际信号已直连 controller)。
        """
        await self._app_state_ctrl.on_notification_fallback(event)

    def _switch_tab(self, idx: int) -> None:
        """2026-10-10 v1.12.x:thin delegate `LifecycleController.switch_tab`。
        保留方法名以保持测试夹具 + 内部 call site 引用稳定。
        """
        self._lifecycle_ctrl.switch_tab(idx)

    def _on_nav_changed(self, idx: int) -> None:
        """2026-10-10 v1.12.x:thin delegate `LifecycleController.on_nav_changed`。
        保留方法名以保持 signal wire 引用稳定(`nav.current_changed.connect`)。
        """
        self._lifecycle_ctrl.on_nav_changed(idx)

    def _focus_search(self) -> None:
        """2026-11-16 v2.0.2:搜索已移除 — 保留方法名以维持 thin delegate
        契约(test_main_window_thin_delegates_preserved),行为降级为切回 LIVE 页。
        """
        self._lifecycle_ctrl.switch_tab(0)

    def _on_global_escape(self) -> None:
        """2026-10-10 v1.12.x:thin delegate `LifecycleController.on_global_escape`。
        保留方法名以保持调用点引用稳定。
        """
        self._lifecycle_ctrl.on_global_escape()

    def _copy_current_message_text(self) -> None:
        """2026-10-10 v1.12.x:thin delegate `LifecycleController.copy_current_message_text`。
        保留方法名以保持调用点引用稳定。
        """
        self._lifecycle_ctrl.copy_current_message_text()

    # ======================== 槽 ========================

    def _on_refresh_channels(self) -> None:
        """2026-10-01 v1.12.x:委托 `SyncController`。"""
        self._sync_ctrl.on_refresh_channels()

    def _on_export(self) -> None:
        """2026-10-01 v1.12.x:委托 `ExportController`。"""
        self._export_ctrl.on_export()

    def _on_detail_copy_text(self, text: str) -> None:
        """详情「复制文本」toast — clipboard 写入已在 MessageDetail 内完成。"""
        self.status_bar.show_message(self.tr("已复制"), 2000)

    # ======================== EventBus 回调 ========================

    async def _on_bus_login(self, e) -> None:
        """2026-10-09 v1.12.x:thin delegate `AppStateController.on_bus_login`。

        保留方法名以保持测试夹具引用稳定(实际信号已直连 controller)。
        """
        await self._app_state_ctrl.on_bus_login(e)

    def _on_bell_clicked(self) -> None:
        """2026-10-09 v1.12.x:thin delegate `AppStateController.on_bell_clicked`。

        保留方法名以保持测试夹具引用稳定(实际信号已直连 controller)。
        """
        self._app_state_ctrl.on_bell_clicked()

    def _clear_error_log(self) -> None:
        """2026-10-09 v1.12.x:thin delegate `AppStateController.on_clear_error_log`。

        保留方法名以保持测试夹具引用稳定(实际信号已直连 controller)。
        """
        self._app_state_ctrl.on_clear_error_log()

    # ======================== VM 事件回调 ========================
    # 2026-10-04 plan 阶段 3.3:VM `message_received` / `message_edited` /
    # `message_interactions_changed` + EventBus `MessageDeleted` 4 handler
    # 抽到 `MessageStreamController`。MainWindow 仍持 `_on_login_state`
    # 等登录/连接/导出/同步委托给 _settings/_sync/_export controller 之外的
    # 杂项 VM signal。

    def _on_login_state(self, state: str) -> None:
        """2026-10-09 v1.12.x:thin delegate `AppStateController.on_login_state`。

        保留方法名以保持测试夹具引用稳定(实际信号已直连 controller)。
        """
        self._app_state_ctrl.on_login_state(state)

    def _on_conn_state(self, state: str) -> None:
        """2026-10-09 v1.12.x:thin delegate `AppStateController.on_conn_state`。"""
        self._app_state_ctrl.on_conn_state(state)

    def _on_export_progress(self, progress: object) -> None:
        """2026-10-01 v1.12.x:委托 `ExportController`。"""
        self._export_ctrl.on_export_progress(progress)

    def _on_sync_progress(self, e) -> None:
        """2026-10-01 v1.12.x:委托 `SyncController`。"""
        self._sync_ctrl.on_sync_progress(e)

    def _on_sync_done(self, e) -> None:
        """2026-10-01 v1.12.x:委托 `SyncController`。"""
        self._sync_ctrl.on_sync_done(e)

    def _on_export_done(self, result: dict | None, error: str | None) -> None:
        """2026-10-01 v1.12.x:委托 `ExportController`。"""
        self._export_ctrl.on_export_done(result, error)

    def _on_error(self, msg: str) -> None:
        """2026-10-09 v1.12.x:thin delegate `AppStateController.on_error`。"""
        self._app_state_ctrl.on_error(msg)

    def _on_settings_changed(
        self,
        what: str,
        needs_relogin: bool,
        needs_restart: bool,
        backend_label: str,
    ) -> None:
        """2026-10-01 v1.12.x:委托 `SettingsReactionController`。"""
        self._settings_reaction_ctrl.on_settings_changed(
            what, needs_relogin, needs_restart, backend_label
        )

    # ======================== 状态刷新 ========================

    def _refresh_state(self) -> None:
        """channels_changed / 登录状态变化 / 定时 触发刷新。"""
        all_known = self._vm.monitored_channels
        # 2026-11-19 hotfix:左栏「已加入频道」不在这里刷。
        # monitored_channels 语义是「仅已监听」(VM bootstrap 从 storage
        # list_monitored_channels 拉),拿它 set_joined 会在每次监听变更
        # (双击加入/移出 → channels_changed → 本方法)时把左栏覆盖成
        # 右栏内容 —— 用户看到「已加入」栏塌缩成与「已监听」相同。
        # 左栏唯一数据源是「刷新」按钮的 refresh_joined_channels →
        # joined_loaded → set_joined,与监听状态解耦。
        subscribed = [ch for cid, ch in all_known.items() if cid in self.monitor.monitored_ids]
        self.channel_panel.set_monitored(subscribed)

        # 2026-11-16:列表 + 详情共用同一份频道名映射(名称优先、id 兜底)。
        titles = {cid: ch.title for cid, ch in all_known.items()}
        self.live_view.set_channel_titles(titles)
        self.message_detail.set_channel_titles(titles)
        # 2026-09-14 v1.7.5 PR #5 (P0-L):订阅数为 0 → no_monitored overlay;
        # > 0 → live_empty(LIVE 流空状态)。搜索无结果 by search handler 切。
        if not subscribed:
            self.live_view.set_empty_state("no_monitored")
        else:
            self.live_view.set_empty_state("live_empty")
        self._vm.load_recent_messages()

        # 更新 dashboard 统计 — 「已加入频道」由 dashboard 异步读库
        # 的 tg_subscribed 计数,不再拿 monitored_channels 凑数。
        self.dashboard.update_stats(len(subscribed))


# ======================== 紧凑头栏 ========================
