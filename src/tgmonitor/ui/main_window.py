"""主窗口 — 导航(左) + 内容页(QStackedWidget)。

架构从「工具栏 + splitter 侧栏」改为「竖向导航 + 四页内容」:

  ┌─────────────────────────────────────────────┐
  │ ●                          🟢 已登录  [登出] │ ← 紧凑头栏
  ├──┬──────────────────────────────────────────┤
  │  │                                           │
  │ 📡  │  QStackedWidget                        │
  │ 实时│   0: 实时流(LIVE) — MessageView 全宽     │
  │    │   1: 大盘(DASHBOARD) — 统计 + 活动        │
  │ 📊  │   2: 频道(CHANNELS) — ChannelWidget     │
  │ 大盘│   3: 设置(SETTINGS) — 整页配置           │
  │    │                                           │
  │ 📋  │                                           │
  │ 频道│                                           │
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
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.dto import MessageDTO, SyncOptions
from tgmonitor.core.events import AuthErrorOccurred, LoginStateChanged
from tgmonitor.core.settings_store import EditableSettings
from tgmonitor.ui.icon import action_icon
from tgmonitor.ui.nav_bar import VerticalNavBar
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel
from tgmonitor.ui.widgets.channel_widget import ChannelWidget
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget
from tgmonitor.ui.widgets.export_dialog import ExportDialog
from tgmonitor.ui.widgets.message_view import MessageView
from tgmonitor.ui.widgets.settings_page import SettingsPage
from tgmonitor.ui.widgets.sync_dialog import (
    SyncOptionsDialog,
    SyncProgressDialog,
)

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)

ShutdownCb = Callable[[], Awaitable[None]]

# ---- 状态映射 ----
_STATE_DOT = {
    "ready": "🟢",
    "error": "🔴",
    "phone_required": "🟡",
    "code_required": "🟡",
    "password_required": "🟡",
    "closed": "⚪",
    "logging_out": "⏳",
    "closing": "⏳",
    "uninit": "⚪",
}
_STATE_LABEL = {
    "ready": "已登录",
    "error": "错误",
    "phone_required": "未登录",
    "code_required": "需验证码",
    "password_required": "需 2FA",
    "closed": "会话关闭",
    "logging_out": "登出中…",
    "closing": "关闭中…",
    "uninit": "启动中…",
}


class MainWindow(QMainWindow):
    def __init__(
        self,
        app: AppService,
        monitor: MonitorService,
        loop: asyncio.AbstractEventLoop,
        env_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.app = app
        self.monitor = monitor
        self.loop = loop
        self.env_path = env_path or Path(".env")
        self.setWindowTitle("tgmonitor · Telegram 频道监听")
        self.resize(1180, 740)

        self._vm = MonitorViewModel(app, monitor, loop)
        self._shutdown_cb: ShutdownCb | None = None
        self._build_ui()
        self._wire_events()
        self._refresh_state()
        self._vm.bootstrap_ui()

    def set_shutdown_callback(self, cb: ShutdownCb) -> None:
        self._shutdown_cb = cb

    # ======================== closeEvent (保持原逻辑) ========================

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._shutdown_cb is not None:
            try:
                import concurrent.futures

                from PySide6.QtWidgets import QApplication

                fut = asyncio.run_coroutine_threadsafe(
                    self._shutdown_cb(), self.loop,
                )
                qt = QApplication.instance()
                deadline = 10.0
                polled = 0.0
                step = 0.05
                while not fut.done():
                    if qt is not None:
                        qt.processEvents()
                    try:
                        fut.result(timeout=step)
                        break
                    except concurrent.futures.TimeoutError:
                        polled += step
                        if polled >= deadline:
                            log.warning(
                                "shutdown timed out after %.1fs; quitting anyway",
                                deadline,
                            )
                            break
                if fut.cancelled():
                    log.warning("shutdown coroutine was cancelled")
                elif fut.done():
                    try:
                        fut.result(timeout=0)
                    except concurrent.futures.CancelledError:
                        log.warning("shutdown coroutine was cancelled (race)")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("shutdown raised: %s: %s",
                                    type(exc).__name__, exc)
            except RuntimeError:
                log.warning("loop unavailable during shutdown")
            except BaseException:  # noqa: BLE001
                log.exception("closeEvent: unexpected error in shutdown")
        super().closeEvent(event)

    # ======================== UI 装配 ========================

    def _build_ui(self) -> None:
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
        self.header = _HeaderBar()
        right_layout.addWidget(self.header)

        # QStackedWidget 内容页
        self.stack = QStackedWidget()
        self.stack.setFrameShape(QFrame.NoFrame)

        # 0: 实时流
        self.live_view = MessageView()
        self.stack.addWidget(self.live_view)

        # 1: 大盘
        self.dashboard = DashboardWidget(self.app, self.monitor)
        self.stack.addWidget(self.dashboard)

        # 2: 频道
        channels_page = QWidget()
        ch_layout = QVBoxLayout(channels_page)
        ch_layout.setContentsMargins(16, 16, 16, 16)
        ch_layout.setSpacing(12)
        ch_title = QLabel("频道管理")
        ch_title.setObjectName("pageTitle")
        ch_layout.addWidget(ch_title)
        self.channel_panel = ChannelWidget(self.app, self.loop)
        ch_layout.addWidget(self.channel_panel, 1)
        self.stack.addWidget(channels_page)

        # 3: 设置
        self.settings_page = SettingsPage(self.app, self.loop, self.env_path)
        self.stack.addWidget(self.settings_page)

        self.stack.setCurrentIndex(0)
        right_layout.addWidget(self.stack, 1)

        # 状态栏
        self.setStatusBar(QStatusBar())
        self.status_bar = self.statusBar()
        self.status_bar.showMessage("就绪")

        root.addWidget(right, 1)
        self.setCentralWidget(central)

        # ---- 信号连接 ----
        self.nav.current_changed.connect(self.stack.setCurrentIndex)
        self.header.btn_logout.clicked.connect(self._on_logout_clicked)
        self.header.btn_action.clicked.connect(self._on_header_action)

        # Dashboard 快速操作
        self.dashboard.on_refresh = self._on_refresh_channels
        self.dashboard.on_export = self._on_export
        self.dashboard.on_sync_all = self._on_sync_all_channels

        # ChannelWidget 信号
        self.channel_panel.btn_refresh.clicked.connect(self._on_refresh_channels)
        self.channel_panel.sync_requested.connect(self._on_sync_requested)

    # ======================== ViewModel 事件绑定 ========================

    def _wire_events(self) -> None:
        self._vm.message_received.connect(self._on_message_received)
        self._vm.login_state.connect(self._on_login_state)
        self._vm.channels_changed.connect(self._refresh_state)
        self._vm.export_done.connect(self._on_export_done)
        self._vm.error.connect(self._on_error)
        self._vm.settings_changed.connect(self._on_settings_changed)

        # 订阅 EventBus 登录状态变化(状态点更新)
        self.app.bus.subscribe(LoginStateChanged, self._on_bus_login)
        self.app.bus.subscribe(AuthErrorOccurred, self._on_bus_auth_error)

    # ======================== 槽 ========================

    def _on_refresh_channels(self) -> None:
        self.status_bar.showMessage("拉取频道列表…", 2000)
        self._vm.refresh_joined_channels()

    def _on_export(self) -> None:
        if not self.monitor.subscribed_ids:
            QMessageBox.information(self, "导出", "请先订阅至少一个频道")
            return
        ids = sorted(int(cid) for cid in self.monitor.subscribed_ids)
        dlg = ExportDialog(self.app, ids, self)
        if dlg.exec():
            self._vm.start_export(dlg.request())

    def _on_sync_all_channels(self) -> None:
        """大盘快速操作:全量同步所有已订阅频道。"""
        ids = list(self.monitor.subscribed_ids)
        if not ids:
            QMessageBox.information(self, "全量同步", "已监听列表为空,先订阅频道")
            return
        self._on_sync_requested(ids)

    def _on_logout_clicked(self) -> None:
        fut = asyncio.run_coroutine_threadsafe(
            self.app.client.logout(), self.loop,
        )

        def _on_done(f) -> None:
            try:
                f.result()
            except Exception as exc:  # noqa: BLE001
                log.exception("logout failed: %s", exc)

        fut.add_done_callback(_on_done)

    def _on_header_action(self) -> None:
        """头栏「登录」按钮 — 弹 LoginDialog(复用现有代码)"""
        from tgmonitor.ui.widgets.login_dialog import LoginDialog
        dlg = LoginDialog(self.app, self.loop, self)
        dlg.exec()
        # 登录成功后刷新状态
        self._refresh_state()

    # ======================== EventBus 回调 ========================

    async def _on_bus_login(self, e) -> None:
        if not isinstance(e, LoginStateChanged):
            return
        self.header.update_state(e.state, e.detail)
        self._refresh_state()

    async def _on_bus_auth_error(self, e) -> None:
        if not isinstance(e, AuthErrorOccurred):
            return
        self.status_bar.showMessage(f"⚠ {e.message}", 5000)

    # ======================== VM 事件回调 ========================

    def _on_message_received(self, m: MessageDTO) -> None:
        self.live_view.set_channel_titles(
            {cid: ch.title for cid, ch in self._vm.known_channels.items()}
        )
        self.live_view.append(m)

    def _on_login_state(self, state: str) -> None:
        self.status_bar.showMessage(f"登录状态: {state}", 4000)

    def _on_export_done(self, result: dict | None, error: str | None) -> None:
        if error:
            QMessageBox.critical(self, "导出失败", error)
        elif result:
            QMessageBox.information(
                self,
                "导出完成",
                f"已写入 {result['out_path']}\n"
                f"{result['message_count']} 条消息,"
                f"{result['bytes_written']} 字节",
            )

    def _on_error(self, msg: str) -> None:
        log.warning("error: %s", msg)
        self.status_bar.showMessage(f"⚠ {msg}", 5000)

    def _on_settings_changed(
        self, what: str, needs_relogin: bool, backend_label: str,
    ) -> None:
        msg = f"已热重载: {what} → {backend_label}"
        self.status_bar.showMessage(msg, 5000)
        if needs_relogin:
            QMessageBox.information(
                self,
                "凭据已变更",
                "Telegram 凭据已变更。\n请重新登录以继续监听。",
            )

    # ======================== 同步请求 ========================

    def _on_sync_requested(self, channel_ids: list[int]) -> None:
        titles: dict[int, str] = {}
        for cid in channel_ids:
            ch = self._vm.known_channels.get(cid)
            titles[cid] = ch.title if ch else f"#{cid}"

        defaults = SyncOptions(
            chat_delay_ms=self.app.settings.sync_chat_delay_ms,
            page_delay_ms=self.app.settings.sync_page_delay_ms,
            resume_from_saved=self.app.settings.sync_resume_from_saved,
        )

        opts_dlg = SyncOptionsDialog(channel_ids, titles, defaults, self)
        if not opts_dlg.exec():
            return
        options = opts_dlg.options()
        if options is None:
            return

        progress_dlg = SyncProgressDialog(
            titles,
            cancel_cb=self.app.channel_sync.cancel,
            parent=self,
        )
        self._vm.sync_progress.connect(progress_dlg.on_progress)
        self._vm.sync_done.connect(progress_dlg.on_done)

        async def _go() -> None:
            try:
                await self.app.sync_channels(channel_ids, options)
            finally:
                try:
                    self._vm.sync_progress.disconnect(progress_dlg.on_progress)
                    self._vm.sync_done.disconnect(progress_dlg.on_done)
                except (RuntimeError, TypeError):
                    pass

        asyncio.run_coroutine_threadsafe(_go(), self.loop)
        progress_dlg.exec()

    # ======================== 状态刷新 ========================

    def _refresh_state(self) -> None:
        """channels_changed / 登录状态变化 / 定时 触发刷新。"""
        all_known = self._vm.known_channels
        self.channel_panel.set_joined(list(all_known.values()))
        subscribed = [
            ch for cid, ch in all_known.items()
            if cid in self.monitor.subscribed_ids
        ]
        self.channel_panel.set_subscribed(subscribed)

        self.live_view.set_channel_titles(
            {cid: ch.title for cid, ch in all_known.items()}
        )
        self._vm.load_recent_messages()

        # 更新 dashboard 统计
        self.dashboard.update_stats(len(all_known), len(subscribed))


# ======================== 紧凑头栏 ========================

class _HeaderBar(QWidget):
    """顶部紧凑信息栏:左标题 + 右登录状态 + 操作。

    不再用 QToolBar,改为自定义 widget,视觉更紧凑。
    """

    btn_logout = None  # type: ignore[assignment]
    btn_action = None  # type: ignore[assignment]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("headerBar")
        self.setFixedHeight(44)

        hbox = QHBoxLayout(self)
        hbox.setContentsMargins(16, 0, 16, 0)
        hbox.setSpacing(8)

        # 左: 标题 / 搜索
        title = QLabel("tgmonitor")
        title.setObjectName("appTitle")
        hbox.addWidget(title)
        hbox.addStretch(1)

        # 右: 状态 + 操作
        self.state_dot = QLabel("⚪")
        self.state_dot.setFixedWidth(20)
        hbox.addWidget(self.state_dot)

        self.state_label = QLabel("就绪")
        self.state_label.setObjectName("headerState")
        hbox.addWidget(self.state_label)

        self.btn_action = QPushButton("登录")
        self.btn_action.setObjectName("headerActionBtn")
        self.btn_action.setVisible(False)
        hbox.addWidget(self.btn_action)

        self.btn_logout = QPushButton("登出")
        self.btn_logout.setObjectName("headerActionBtn")
        self.btn_logout.setVisible(False)
        hbox.addWidget(self.btn_logout)

    def update_state(self, state: str, detail: str = "") -> None:
        dot = _STATE_DOT.get(state, "⚪")
        label = _STATE_LABEL.get(state, state)
        if state == "error" and detail:
            label = f"{label}:{detail[:40]}"

        self.state_dot.setText(dot)
        self.state_label.setText(label)

        # 根据状态显隐操作按钮
        if state == "ready":
            self.btn_action.setVisible(False)
            self.btn_logout.setVisible(True)
        elif state in ("phone_required", "closed", "uninit"):
            self.btn_action.setText("登录")
            self.btn_action.setVisible(True)
            self.btn_logout.setVisible(False)
        elif state in ("code_required",):
            self.btn_action.setText("验证码")
            self.btn_action.setVisible(True)
            self.btn_logout.setVisible(False)
        elif state in ("password_required",):
            self.btn_action.setText("2FA 密码")
            self.btn_action.setVisible(True)
            self.btn_logout.setVisible(False)
        else:
            self.btn_action.setVisible(False)
            self.btn_logout.setVisible(False)
