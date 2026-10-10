"""主窗口 — 导航(左) + 内容页(QStackedWidget)。

架构从「工具栏 + splitter 侧栏」改为「竖向导航 + 五页内容」:

  ┌─────────────────────────────────────────────┐
  │ ●                          🟢 已登录  [登出] │ ← 紧凑头栏
  ├──┬──────────────────────────────────────────┤
  │  │                                           │
  │ 📡  │  QStackedWidget                        │
  │ 实时│   0: 实时流(LIVE) — MessageView 全宽     │
  │    │   1: 大盘(DASHBOARD) — 统计 + 活动        │
  │ 📊  │   2: 频道(CHANNELS) — ChannelWidget     │
  │ 大盘│   3: 媒体管理(MEDIA) — MediaManagerWidget│
  │    │   4: 设置(SETTINGS) — 整页配置           │
  │ 📋  │                                           │
  │ 频道│                                           │
  │    │                                           │
  │ 💾  │                                           │
  │ 媒体│                                           │
  │    │                                           │
  │ ⚙  │                                           │
  │ 设置│                                           │
  ├──┴──────────────────────────────────────────┤
  │ 🟢 Ready · 3 channels · 0 new               │ ← 状态栏
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
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.events import (
    LoginStateChanged,
    NotificationRequested,
    QuitRequested,
)
from tgmonitor.ui.async_bridge import run_coro
from tgmonitor.ui.controllers import (
    AppStateController,
    ExportController,
    HeaderActionController,
    LifecycleController,
    LiveSelectionController,
    MainWindowCtx,
    MediaActionsController,
    MessageStreamController,
    SearchController,
    SettingsReactionController,
    ShortcutController,
    SyncController,
    SyncFlowController,
    ThemeController,
    TrayController,
)
from tgmonitor.ui.nav_bar import VerticalNavBar
from tgmonitor.ui.shutdown import run_shutdown_coro_sync
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel
from tgmonitor.ui.widgets.channel_widget import ChannelWidget
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget
from tgmonitor.ui.widgets.header_bar import HeaderBar
from tgmonitor.ui.widgets.media_manager_widget import MediaManagerWidget
from tgmonitor.ui.widgets.message_detail import MessageDetail
from tgmonitor.ui.widgets.message_view import MessageView
from tgmonitor.ui.widgets.selection_toolbar import SelectionToolbar
from tgmonitor.ui.widgets.settings_page import SettingsPage
from tgmonitor.ui.widgets.tray_icon import TrayIcon

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)

ShutdownCb = Callable[[], Awaitable[None]]

# ---- 状态映射 ----

# 2026-09-08 v1.7.0:右键菜单 / 详情按钮传上来的 items 形状是
# list[tuple[cid, mid]] 或 list[(cid, mid, idx)](media manager 风格)。
# LIVE 多选统一成 list[tuple[int, int]] 传给 AppService。
# 2026-10-04 plan 阶段 3.3:_normalize_selection_items 抽到
# `LiveSelectionController`(只有 live_selection 用)。


class MainWindow(QMainWindow):
    """应用主窗口:左导航 + 5 页内容 + 紧凑头栏 + 状态栏。

    # 5 个 page 由 QStackedWidget 持有:
    #   0 LIVE      → MessageView + MessageDetail(实时流 + 详情)
    #   1 DASHBOARD → 统计 + 活动时间线 + 快速操作
    #   2 CHANNELS  → ChannelWidget(订阅 / 退订 / 全量同步)
    #   3 MEDIA     → MediaManagerWidget(浏览 / 重试 / 删 / 打开 + prune)
    #   4 SETTINGS  → 整页配置(凭据 / 存储 / 代理 / 媒体 / 同步)
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
        self._build_ui()
        self._build_menu()
        self._build_tray()
        self._wire_events()
        self._wire_theme_change_signal()
        self._refresh_state()
        self._vm.bootstrap_ui()
        self._setup_controllers()
        # 2026-10-09 v1.12.x 下一 plan 目标 1.3:快捷键绑定挪到
        # `_setup_controllers` 之后,因 `ShortcutController` 在那里实例化
        # 且需要 `act_show` / `act_quit`(`_build_menu` 必建) + `self._ctx`
        # 已就位。
        self._wire_shortcuts()

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
        # 2026-08-30 v1.5.0 PR #A4:TrayIcon 实例。`is_active=False` 时
        # (offscreen / Linux 无 indicator)closeEvent 不进入 minimize-to-tray。
        self._tray: TrayIcon | None = None
        # 2026-08-30 PR #A4:tray「退出」→ VM 收到 QuitRequested → emit
        # `quit_requested` signal → 此 handler → 真退出 qt_app.quit。
        self._vm.quit_requested.connect(self._on_vm_quit_requested)
        # 2026-09-03 v1.6.1:暂停 / 恢复信号 — status bar label + window title 后缀
        self._vm.monitoring_paused.connect(self._on_monitoring_paused)
        self._vm.monitoring_resumed.connect(self._on_monitoring_resumed)
        # 2026-09-08 v1.7.0:LIVE 多选 toolbar 缓存最近一次 selection —
        # 2026-10-04 plan 阶段 3.3:挪到 `LiveSelectionController._last_live_selection`
        # (MainWindow 不再持有)。

    def _setup_controllers(self) -> None:
        """2026-10-04 plan 阶段 3.2:装配 MainWindowCtx + 6 controller。

        controller 间无顺序依赖(各自只读 ctx 字段),按当前 alphabetical
        顺序排(export / header_action / settings_reaction / theme / tray / sync)。

        2026-10-04 plan 阶段 3.3:搜索 domain 全部就近 — debounce timer 创建
        + MainWindowCtx 装配 + 7 个 signal wire + SearchController 实例化都
        在本方法内,避免跨 `_wire_signals` / `_setup_controllers` 拆碎。
        """

        # 2026-09-02 v1.5.2 PR #B5:300ms debounce — 文本输入 300ms 内无新
        # 字符才触发 vm.search_messages。parent=self 确保 MainWindow 关闭
        # 时 timer deleteLater 自然清理,无悬挂。
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(300)

        # ======================== 6 controller 装配(2026-10-01 v1.12.x) ========================
        # 复杂 4 个(live_selection / media_actions / search / message_stream)
        # 留 main_window.py 自身。
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
            selection_toolbar=self._selection_toolbar,
            dashboard=self.dashboard,
            channel_panel=self.channel_panel,
            header=self.header,
            status_bar=self.status_bar,
            nav=self.nav,
            stack=self.stack,
            tray=self._tray,
            search_debounce=self._search_debounce,
            media_refresh_debounce=self._media_refresh_debounce,
        )
        self._theme_ctrl = ThemeController(self._ctx)
        self._tray_ctrl = TrayController(self._ctx)
        self._header_action_ctrl = HeaderActionController(self._ctx)
        self._settings_reaction_ctrl = SettingsReactionController(self._ctx)
        self._export_ctrl = ExportController(self._ctx)
        self._sync_ctrl = SyncController(self._ctx)
        # 2026-10-09 v1.12.x 下一 plan 目标 1.1:抽 `SyncFlowController` —
        # 接管 `_on_sync_requested` 38 行 orchestrator + 3 helper。SyncController
        # 仍走 `_on_sync_all_channels` 委派 main_window thin delegate。
        self._sync_flow_ctrl = SyncFlowController(self._ctx)
        # 2026-10-09 plan 目标 1.2:抽 `AppStateController` — 接管 C+D 类 82 LOC
        # (VM login/conn/error + EventBus notification/login + bell/clear)。
        self._app_state_ctrl = AppStateController(self._ctx)
        # 2026-10-09 plan 目标 1.3:抽 `ShortcutController` — 接管 F 类 94 LOC
        # (`_wire_shortcuts` 38 + `reload_shortcuts` 40 + `_ACTION_SLOTS` 16)。
        # 实际 bind 由 `__init__` 末尾的 `_wire_shortcuts()` thin delegate 触发。
        self._shortcut_ctrl = ShortcutController(self._ctx)
        # 2026-10-10 plan 目标 1.4:抽 `LifecycleController` — 接管 F 类 110 LOC
        # (show_and_raise / quit_app / switch_tab / on_nav_changed /
        # focus_search / on_global_escape / copy_current_message_text)。
        self._lifecycle_ctrl = LifecycleController(self._ctx)
        # 2026-10-04 plan 阶段 3.3:搜索 controller(模板)— 抽 8 个 handler
        # (`_on_search_*` + `_run_search_query`)到独立 plain class。
        self._search_ctrl = SearchController(self._ctx)
        # 搜索 signal wire(controller 实例化之后立即连,顺序敏感:
        # 若 `_search_ctrl` 未就位就调 `.on_search_*` 会触发 AttributeError)。
        self._search_debounce.timeout.connect(self._search_ctrl.on_debounce_fire)
        self.header.search_bar.text_changed.connect(self._search_ctrl.on_search_changed)
        # 2026-09-02 v1.5.2 PR #B5:date_from/to 折叠面板变化 → 立即拉
        # (不需要 debounce — 用户主动调日历 = 期望立刻响应)
        self.header.search_bar.date_changed.connect(self._search_ctrl.on_search_date_changed)
        # 2026-09-03 v1.5.3 PR #D2:scope toggle(已订阅/全部)→ 走同一
        # debounce 重拉(不 clear view,让用户对比已订 vs 全部结果)
        self.header.search_bar.scope_changed.connect(self._search_ctrl.on_search_scope_changed)
        # 2026-09-14 v1.7.5 PR #8:★/🏷/📌 3 filter toggle → 走同一 debounce
        # 重拉(不 clear view,即时 LIVE narrow + 异步权威结果到位)。
        self.header.search_bar.favorite_toggled.connect(
            self._search_ctrl.on_search_favorite_toggled
        )
        self.header.search_bar.tag_toggled.connect(self._search_ctrl.on_search_tag_toggled)
        self.header.search_bar.pinned_toggled.connect(self._search_ctrl.on_search_pinned_toggled)
        # 2026-10-04 plan 阶段 3.3:live_selection controller — 抽 14 _on_live_*
        # handler + 7 _run_live_* helper + _show_batch_progress_dialog +
        # _normalize_selection_items 到独立 plain class。controller 自持
        # `_last_live_selection` 共享 state。
        self._live_selection_ctrl = LiveSelectionController(self._ctx)
        # ---- LIVE 多选 signal wire(顺序敏感:controller 先实例化) ----
        # selection state
        self.live_view.selection_count_changed.connect(
            self._live_selection_ctrl.on_selection_count_changed
        )
        self.live_view.selection_messages_changed.connect(
            self._live_selection_ctrl.on_selection_messages_changed
        )
        # 右键菜单 6 入口(带 items)
        self.live_view.export_requested.connect(self._live_selection_ctrl.on_live_export)
        self.live_view.delete_requested.connect(self._live_selection_ctrl.on_live_delete)
        self.live_view.mark_read_requested.connect(self._live_selection_ctrl.on_live_mark_read)
        # 2026-09-09 v1.7.2:用户元数据右键菜单 → AppService facade。
        self.live_view.favorite_requested.connect(self._live_selection_ctrl.on_live_favorite)
        self.live_view.tags_requested.connect(self._live_selection_ctrl.on_live_tags)
        self.live_view.notes_requested.connect(self._live_selection_ctrl.on_live_notes)
        # toolbar 6 按钮(读 controller 内部 _last_live_selection)
        self._selection_toolbar.export_clicked.connect(
            self._live_selection_ctrl.on_live_export_selected
        )
        self._selection_toolbar.delete_clicked.connect(
            self._live_selection_ctrl.on_live_delete_selected
        )
        self._selection_toolbar.mark_read_clicked.connect(
            self._live_selection_ctrl.on_live_mark_read_selected
        )
        # 2026-09-09 v1.7.2:批量动作扩展 — 转发 / 钉选 / 表情回应。
        self._selection_toolbar.forward_clicked.connect(
            self._live_selection_ctrl.on_live_forward_selected
        )
        self._selection_toolbar.pin_clicked.connect(self._live_selection_ctrl.on_live_pin_selected)
        self._selection_toolbar.react_clicked.connect(
            self._live_selection_ctrl.on_live_react_selected
        )
        # 详情顶部 3 按钮 — 单条操作入口
        self.message_detail.export_current_requested.connect(
            self._live_selection_ctrl.on_live_export_current
        )
        self.message_detail.delete_current_requested.connect(
            self._live_selection_ctrl.on_live_delete_current
        )
        self.message_detail.copy_text_requested.connect(self._live_selection_ctrl.on_live_copy_text)

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

    # ======================== closeEvent (保持原逻辑) ========================

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """窗口关闭 — 同步阻塞等 async shutdown(≤10s 超时)再放行。

        2026-09-23 v1.8.x:`run_shutdown_coro_sync` helper 改为永远走
        `fut.result(timeout)` 阻塞等,不嵌套 QEventLoop pump —
        macOS 26.x arm64 CI 镜像(`macos-26-arm64` 20260728)嵌套 exec
        偶发 segfault,Windows 真机平台插件更稳定触发。helper 在
        qasync 主线程上调用 `run_coroutine_threadsafe`,协程被调度到
        qasync loop(同线程,但 Qt 不在主线程 pump 它——qasync 自己 tick);
        主线程只 `fut.result(timeout)` 等,Qt 事件分发暂停但 shutdown
        协程仍能推进。

        任何意外(`RuntimeError` / `BaseException` 含 `CancelledError`)由
        最外层 try/except 兜底 — Qt 的 `closeEvent` 不应让 Python 异常抛回
        主循环,否则会变 "Error calling Python override" 把窗口关成 fail。
        """
        # 2026-08-30 v1.5.0 PR #A4:首次 close(用户点 X / Alt+F4)默认
        # 最小化到 tray,不退出。File→Quit / tray「退出」菜单走
        # `qt_app.quit()` → `aboutToQuit` → 关闭全部窗口(到这一步时
        # `_truly_quit=True`),这里才走 shutdown。
        if not self._truly_quit:
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
        """
        self._build_central_widget()
        self._wire_signals()

    def _build_central_widget(self) -> None:
        """装配 central widget + 5 个 stacked page + status bar + 2 个 debounce timer。

        2026-10-04 plan 阶段 3.1:从原 `_build_ui` 拆出。装配关系复杂,信号
        连接全部留在 `_wire_signals`。
        """
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 左: 导航栏 ----
        self.nav = VerticalNavBar()
        root.addWidget(self.nav)

        # ---- 右: 头栏 + 内容 + 状态栏 ----
        right = QWidget()
        right.setObjectName("contentArea")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # 紧凑头栏
        self.header = HeaderBar()
        right_layout.addWidget(self.header)

        # QStackedWidget 内容页
        self.stack = QStackedWidget()
        self.stack.setFrameShape(QFrame.NoFrame)

        # 0: 实时流(MessageView + MessageDetail 横向并排)
        # 2026-09-08 v1.7.0:LIVE 顶部增加 _SelectionToolbar(默认 hidden,
        # 多选 >0 才 show)。从单层 QHBoxLayout → QVBoxLayout 嵌套:
        # 上 toolbar / 下 body(QSplitter[live_view, message_detail])。
        # 2026-09-14 v1.7.5 PR #4 (P0-H):QHBoxLayout → QSplitter,用户可拖
        # 详情面板宽度自由(原 280-420 锁死)。splitter 比例 2:1(live 占大头)。
        live_page = QWidget()
        live_layout = QVBoxLayout(live_page)
        live_layout.setContentsMargins(0, 0, 0, 0)
        live_layout.setSpacing(0)
        self._selection_toolbar = SelectionToolbar()
        self._selection_toolbar.setVisible(False)
        live_layout.addWidget(self._selection_toolbar)
        body = QSplitter(Qt.Horizontal)
        body.setContentsMargins(0, 0, 0, 0)
        body.setHandleWidth(1)
        body.setChildrenCollapsible(False)  # 防止用户拖到极窄被隐
        self.live_view = MessageView()
        self.message_detail = MessageDetail()
        body.addWidget(self.live_view)
        body.addWidget(self.message_detail)
        body.setStretchFactor(0, 2)  # live 优先占 2/3
        body.setStretchFactor(1, 1)  # detail 占 1/3
        body.setSizes([800, 400])  # 启动默认(800px wide → 详情 400)
        live_layout.addWidget(body, 1)
        self.stack.addWidget(live_page)

        # 1: 大盘
        self.dashboard = DashboardWidget(self.app, self.monitor, loop=self.loop)
        self.stack.addWidget(self.dashboard)

        # 2: 频道
        channels_page = QWidget()
        ch_layout = QVBoxLayout(channels_page)
        ch_layout.setContentsMargins(16, 16, 16, 16)
        ch_layout.setSpacing(12)
        ch_title = QLabel(self.tr("频道管理"))
        ch_title.setObjectName("pageTitle")
        ch_layout.addWidget(ch_title)
        self.channel_panel = ChannelWidget(self.app, self.loop)
        ch_layout.addWidget(self.channel_panel, 1)
        self.stack.addWidget(channels_page)

        # 3: 媒体管理(Media Manager) — 浏览 / 重试 / 删 / 打开 + orphan reconcile
        media_page = QWidget()
        media_layout = QVBoxLayout(media_page)
        media_layout.setContentsMargins(0, 0, 0, 0)
        media_layout.setSpacing(0)
        self.media_manager = MediaManagerWidget()
        media_layout.addWidget(self.media_manager)
        self.stack.addWidget(media_page)

        # 4: 设置
        self.settings_page = SettingsPage(self.app, self.loop, self.env_path)
        self.stack.addWidget(self.settings_page)

        self.stack.setCurrentIndex(0)
        right_layout.addWidget(self.stack, 1)

        # 自定义状态栏(2026-10-01 v1.11.x 组件化重构) — 替代 Qt QStatusBar,
        # 9 个子组件(LEFT 5 + RIGHT 4)由 `StatusBar` widget 自管;MainWindow
        # 只做 1 行装配 + 后续 signal 绑 setter。
        from tgmonitor.ui.widgets.status_bar import StatusBar

        self.status_bar = StatusBar(
            self.app,
            self._vm,
            objects_error=self._objects_error,
            parent=self,
        )

        # Media Manager 批量删除 debounce(2026-09-28 fix):100 条连续 MediaDeleted
        # 只刷一次 widget。timer 放在 MainWindow 上,与 StatusBar 解耦。
        # 2026-10-04 plan 阶段 3.3:`timeout` 连接移到 `_setup_controllers`,
        # 跟 `MediaActionsController.flush_media_refresh_pending` 接。

        self._media_refresh_debounce = QTimer(self)
        self._media_refresh_debounce.setSingleShot(True)
        self._media_refresh_debounce.setInterval(100)

        # 启动提示 — 替代旧 `status_bar.showMessage(self.tr("就绪"))`
        self.status_bar.show_message(self.tr("就绪"), 3000)

        root.addWidget(right, 1)
        self.setCentralWidget(central)

    def _wire_signals(self) -> None:
        """2026-10-04 plan 阶段 3.1:集中所有信号连接(原 `_build_ui` 后半段)。

        5 大类:
        - nav / header / search bar / theme → MainWindow 内部槽
        - Dashboard / ChannelWidget → 业务回调
        - MediaManagerWidget → vm 或本地回调
        - MessageView / MessageDetail → selection + 详情
        - SelectionToolbar + 详情顶部按钮 → 批量动作入口
        """
        # ---- 信号连接 ----
        self.nav.current_changed.connect(self.stack.setCurrentIndex)
        # Media Manager 页切换时自动拉一次列表(VM 异步,首次可能空 → 等频道就绪)
        self.nav.current_changed.connect(self._on_nav_changed)
        self.header.btn_logout.clicked.connect(self._on_logout_clicked)
        self.header.btn_action.clicked.connect(self._on_header_action)
        # 2026-10-04 plan 阶段 3.3:搜索信号连接在 `_setup_controllers` 里
        # 完成(controller 实例化之后立即连),避免 _wire_signals 提前引用
        # `_search_ctrl` 触发 AttributeError。
        self.header.btn_theme.clicked.connect(self._on_theme_toggle)

        # Dashboard 快速操作
        self.dashboard.on_refresh = self._on_refresh_channels
        self.dashboard.on_export = self._on_export
        self.dashboard.on_sync_all = self._on_sync_all_channels

        # ChannelWidget 信号
        self.channel_panel.btn_refresh.clicked.connect(self._on_refresh_channels)
        self.channel_panel.sync_requested.connect(self._on_sync_requested)

        # MessageView → MessageDetail(点击消息显示详情)
        self.live_view.message_selected.connect(self.message_detail.show_message)

        # 2026-09-08 v1.7.0:多选浮层 + 详情顶部 3 按钮 — 走同一 selection
        # 入口。toolbar 默认 hidden,count>0 时 show 并更新计数 label;
        # count=0 时 hide(selectionCleared 也走同一槽,toolbar.clear 复用)。
        # 2026-10-04 plan 阶段 3.3:全部委托 `LiveSelectionController`,实际
        # 连接在 `_setup_controllers` 完成(controller 实例化后立即连,顺序
        # 敏感:避免 `_wire_signals` 提前引用 `_live_selection_ctrl` 触发
        # AttributeError,同 `SearchController` 模式)。
        self._selection_toolbar.clear_clicked.connect(self.live_view.clear_selection)

    def _build_menu(self) -> None:
        """File menu — 「显示主窗口 / 暂停监听 / 退出」(2026-08-30 v1.5.0 PR #A4)。

        「暂停监听」对应 tray menu 同名动作 — 留作 v1.5.1,本 PR 行为同
        tray 一致(发 QuitRequested(pause=True),目前无订阅者,只 log)。
        「退出」走 `qt_app.quit()` → aboutToQuit 触发 setQuitOnLastWindowClosed
        之前的 `window.close()`,此时 `_truly_quit=True` 已置,closeEvent
        不会再 minimize-to-tray,直接走 shutdown。

        2026-09-07 v1.6.9:`act_show` / `act_quit` 改成 self. 属性(原局部变量),
        让 `_bind_shortcuts` 能 reload `setShortcut()`。`setShortcut()` 初始
        值仍是硬编码默认值,稍后 `_wire_shortcuts()` 调一次覆盖成 Settings
        实际值(默认 = 硬编码,所以行为不变)。
        """
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("&File")
        self.act_show = QAction(self.tr("显示主窗口"), self)
        self.act_show.setShortcut(QKeySequence("Ctrl+0"))
        self.act_show.triggered.connect(self._show_and_raise)
        file_menu.addAction(self.act_show)
        file_menu.addSeparator()
        act_pause = QAction(self.tr("暂停监听"), self)
        act_pause.triggered.connect(
            lambda: self.app.bus.publish_threadsafe(self.loop, QuitRequested(pause=True))
        )
        file_menu.addAction(act_pause)
        self.act_quit = QAction(self.tr("退出"), self)
        self.act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        self.act_quit.triggered.connect(self._quit_app)
        file_menu.addAction(self.act_quit)

    def _build_tray(self) -> None:
        """系统托盘图标(2026-08-30 v1.5.0 PR #A4)。

        `isSystemTrayAvailable()` 假时(offscreen / Linux 无 indicator)— `_tray`
        仍创建(订阅 handler 内部 no-op),但 `is_active=False`,closeEvent
        不进入 minimize-to-tray 分支,直接走 shutdown。
        """
        self._tray = TrayIcon(self, self.app)
        if self._tray.is_active:
            self._tray.show()
        # 2026-10-09 v1.12.x:NotificationRequested 订阅挪到 `_wire_signals`
        # 阶段(原 `_build_tray` 早于 `_setup_controllers`,controller 还未
        # 实例化)。TrayIcon 内部仍订一份做系统通知;此处补订做状态栏兜底。

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

    def _wire_shortcuts(self) -> None:
        """2026-10-09 v1.12.x:thin delegate `ShortcutController.bind_shortcuts`。
        保留方法名以保持测试夹具引用稳定。
        """
        self._shortcut_ctrl.bind_shortcuts()

    def reload_shortcuts(self, settings: Any) -> None:
        """v1.6.9:SettingsPage「保存并应用」成功后调此热重载快捷键 — 2026-10-09
        转给 `ShortcutController.reload_shortcuts`。
        """
        self._shortcut_ctrl.reload_shortcuts(settings)

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
        """2026-10-10 v1.12.x:thin delegate `LifecycleController.focus_search`。
        保留方法名以保持 `_ACTION_SLOTS` 工厂引用稳定。
        """
        self._lifecycle_ctrl.focus_search()

    def _on_theme_toggle(self) -> None:
        """切换浅色/暗色主题 — 委托 `ThemeController`。(2026-10-01 v1.12.x)"""
        self._theme_ctrl.toggle()

    def _wire_theme_change_signal(self) -> None:
        """2026-08-30 v1.5.0 PR #A5:ThemeManager.theme_changed → nav_bar 重画。

        监听 ThemeManager 的全局 signal(SYSTEM 态 OS 切色时
        `_on_system_scheme_changed` 也会 emit)— nav bar / channel panel
        按新主题重 tint,免得 OS 切色后图标颜色 stale。
        """
        from tgmonitor.ui.theme import ThemeManager

        ThemeManager._instance().theme_changed.connect(self._on_theme_changed)

    def _on_theme_changed(self) -> None:
        """2026-08-30 v1.5.0 PR #A5:ThemeManager 主题变 → UI 同步 — 委托 controller。"""
        self._theme_ctrl.on_theme_changed()

    def _on_global_escape(self) -> None:
        """2026-10-10 v1.12.x:thin delegate `LifecycleController.on_global_escape`。
        保留方法名以保持 `_ACTION_SLOTS` 工厂引用稳定。
        """
        self._lifecycle_ctrl.on_global_escape()

    def _copy_current_message_text(self) -> None:
        """2026-10-10 v1.12.x:thin delegate `LifecycleController.copy_current_message_text`。
        保留方法名以保持 `_ACTION_SLOTS` 工厂引用稳定。
        """
        self._lifecycle_ctrl.copy_current_message_text()

    # ======================== ViewModel 事件绑定 ========================

    def _wire_events(self) -> None:
        # 2026-10-04 plan 阶段 3.3:VM message_received / message_edited /
        # message_interactions_changed → `MessageStreamController`(signal wire
        # 在 `_setup_controllers`,避免提前引用 `_message_stream_ctrl`)。
        # EventBus `MessageDeleted` 也改在 `_setup_controllers` 段订阅。
        # 2026-10-04 plan 阶段 3.3:VM media_downloaded / media_retried /
        # media_deleted → `MediaActionsController`(signal wire 在
        # `_setup_controllers`,避免提前引用 `_media_actions_ctrl`)。
        # 2026-09-01 v1.5.1 PR #B3:下载进度反馈 — VM signal → media_manager
        # 直接刷新对应 row 的「已下载 X / Y (Z%)」文字。
        self._vm.media_download_progress.connect(self.media_manager.on_download_progress)
        # 2026-10-09 v1.12.x:VM login/conn/error wire 挪到 `_wire_signals` 阶段
        # (原 `_wire_events` 早于 `_setup_controllers`,controller 还未实例化)。
        self._vm.channels_changed.connect(self._refresh_state)
        self._vm.export_done.connect(self._on_export_done)
        self._vm.settings_changed.connect(self._on_settings_changed)
        # 2026-09-25 v1.8.x:同步/导出进度 → 状态栏左侧活动指示器。
        # 之前 sync_progress / export_progress 只绑 dialog,MainWindow 不感知,
        # 现在多连一份到 _on_sync_progress / _on_export_progress — 不影响
        # dialog(dialog 自己接的 signal 仍生效,Qt signal 多订阅互不干扰)。
        self._vm.sync_progress.connect(self._on_sync_progress)
        self._vm.sync_done.connect(self._on_sync_done)
        self._vm.export_progress.connect(self._on_export_progress)
        # 2026-09-02 v1.5.2 PR #B5:VM 搜索结果 → 批量替换 LIVE view。
        # vm.search_messages(...) 完成后 emit `message_search_results(list[MessageDTO])`,
        # MainWindow 接到直接 `set_messages` 替换视图(覆盖原有 LIVE 流)。
        self._vm.message_search_results.connect(self.live_view.set_messages)
        # Media Manager 转发(2026-08-24)
        self._vm.media_list_loaded.connect(self.media_manager.on_media_loaded)
        self._vm.media_reconcile_done.connect(self.media_manager.on_reconcile_done)
        # 按频道批量删除反馈(2026-08-25 PR #4)
        self._vm.channel_cleared.connect(self.media_manager.on_channel_cleared)
        # 缩略图 VM signal — 直接绑 widget(2026-08-25 PR #1)
        self.media_manager.set_view_model(self._vm)

        # 2026-10-09 v1.12.x:`AppStateController` 5 个 wire 全部挪到
        # `_wire_signals` 阶段(原 `_wire_events` 早于 `_setup_controllers`,
        # controller 还未实例化):VM login/conn/error + EventBus login +
        # status_bar._bell.bell_clicked + NotificationRequested。

    # ======================== 槽 ========================

    def _on_refresh_channels(self) -> None:
        """2026-10-01 v1.12.x:委托 `SyncController`。"""
        self._sync_ctrl.on_refresh_channels()

    def _on_export(self) -> None:
        """2026-10-01 v1.12.x:委托 `ExportController`。"""
        self._export_ctrl.on_export()

    def _on_sync_all_channels(self) -> None:
        """大盘快速操作:全量同步所有已订阅频道 — 委托 `SyncController`(v1.12.x)。"""
        self._sync_ctrl.on_sync_all_channels()

    def _on_logout_clicked(self) -> None:
        run_coro(self.loop, self.app.client.logout(), error_label="logout")

    def _on_header_action(self) -> None:
        """头栏「登录」按钮 — 弹 LoginDialog。委托 `HeaderActionController`(v1.12.x)。"""
        self._header_action_ctrl.on_header_action()

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

    # ======================== 同步请求 ========================

    def _on_sync_requested(self, channel_ids: list[int]) -> None:
        """同步请求入口 thin delegate — 委托 `SyncFlowController`(2026-10-09)。"""
        self._sync_flow_ctrl.on_sync_requested(channel_ids)

    # ======================== 状态刷新 ========================

    def _refresh_state(self) -> None:
        """channels_changed / 登录状态变化 / 定时 触发刷新。"""
        all_known = self._vm.known_channels
        self.channel_panel.set_joined(list(all_known.values()))
        subscribed = [ch for cid, ch in all_known.items() if cid in self.monitor.subscribed_ids]
        self.channel_panel.set_subscribed(subscribed)

        self.live_view.set_channel_titles({cid: ch.title for cid, ch in all_known.items()})
        # 2026-09-14 v1.7.5 PR #5 (P0-L):订阅数为 0 → no_subscribed overlay;
        # > 0 → live_empty(LIVE 流空状态)。搜索无结果 by search handler 切。
        if not subscribed:
            self.live_view.set_empty_state("no_subscribed")
        else:
            self.live_view.set_empty_state("live_empty")
        self._vm.load_recent_messages()

        # 更新 dashboard 统计
        self.dashboard.update_stats(len(all_known), len(subscribed))


# ======================== 紧凑头栏 ========================
