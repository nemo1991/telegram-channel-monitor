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
# mypy: disable-error-code="attr-defined"
# PySide6 6.6+.pyi 漏了一堆 class-level const(QFrame.NoFrame / Qt.UserRole /
# QHeaderView.ResizeToContents / QDialogButtonBox.Ok/AcceptRole / ...)— 全
# ui 文件统一关掉 attr-defined,留下 union-attr / var-annotated 等真错要清。
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Coroutine

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
from tgmonitor.core.events import (
    AuthErrorOccurred,
    LoginStateChanged,
    MediaDownloaded,
)
from tgmonitor.ui._async import run_coro
from tgmonitor.ui.nav_bar import VerticalNavBar
from tgmonitor.ui.state_labels import state_dot, state_label
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel
from tgmonitor.ui.widgets.channel_widget import ChannelWidget
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget
from tgmonitor.ui.widgets.export_dialog import ExportDialog
from tgmonitor.ui.widgets.message_detail import MessageDetail
from tgmonitor.ui.widgets.message_view import MessageView
from tgmonitor.ui.widgets.search_bar import SearchBar
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


class MainWindow(QMainWindow):
    """应用主窗口:左导航 + 4 页内容 + 紧凑头栏 + 状态栏。

    # 4 个 page 由 QStackedWidget 持有:
    #   0 LIVE      → MessageView + MessageDetail(实时流 + 详情)
    #   1 DASHBOARD → 统计 + 活动时间线 + 快速操作
    #   2 CHANNELS  → ChannelWidget(订阅 / 退订 / 全量同步)
    #   3 SETTINGS  → 整页配置(凭据 / 存储 / 代理 / 媒体 / 同步)
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
    ) -> None:
        """构造主窗口 + 装配子 widget + 连信号 + 触发初始刷新。

        `env_path` fallback 跟 `app.py` 同步:platform-native
        (`~/.local/share/tgmonitor/.env` 或 `~/Library/Application Support/tgmonitor/.env`),
        不依赖 cwd。
        """
        super().__init__()
        self.app = app
        self.monitor = monitor
        self.loop = loop
        # v1.0.1:env_path fallback 跟 app.py 同步 — platform-native
        # (~/.local/share/tgmonitor/.env / ~/Library/Application Support/tgmonitor/.env),
        # 不依赖 cwd。
        from tgmonitor.core.config import _user_data_dir
        self.env_path = env_path or (_user_data_dir() / ".env")
        self.setWindowTitle("tgmonitor · Telegram 频道监听")
        self.resize(1180, 740)

        self._vm = MonitorViewModel(app, monitor, loop)
        self._shutdown_cb: ShutdownCb | None = None
        self._build_ui()
        self._wire_shortcuts()
        self._wire_events()
        self._refresh_state()
        self._vm.bootstrap_ui()

    def set_shutdown_callback(self, cb: ShutdownCb) -> None:
        """由 `app.py` 在主循环开始时注入 — closeEvent 触发时调起。"""
        self._shutdown_cb = cb

    # ======================== closeEvent (保持原逻辑) ========================

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """窗口关闭 — 同步阻塞等 async shutdown(≤10s 超时)再放行。

        按平台选等待策略:

        **offscreen(测试 / CI)** — 不 pump Qt 事件,直接 `fut.result(timeout)`:
          - macOS 26.x arm64 CI runner(`macos-26-arm64` 20260728 镜像)上,
            closeEvent 里嵌套 `QEventLoop.exec()` / `processEvents()` 偶发
            segfault — offscreen QPA 没有真实 run loop,嵌套 Cocoa run loop
            触发 Qt native race(本地 M2 不触发,是 VM 镜像特有;3/4 CI 跑挂)。
          - 测试的 `self.loop` 是**独立后台线程**的 asyncio loop,`fut.result(
            timeout)` 无需 pump 即可推进 → 这条路径零 Qt 事件分发,天然避开
            native race。

        **真机(cocoa / xcb / windows)** — 嵌套 `QEventLoop` pump:
          - production 用 `qasync.QEventLoop` 当主线程 loop,closeEvent 与
            loop 同线程,必须 pump 才能推进 shutdown coroutine。
          - 两种触发源让 `subloop.quit()` 唤醒主线程:
            - `fut.add_done_callback`:loop 线程里
              `QMetaObject.invokeMethod(..., QueuedConnection)` 跨线程派 quit
            - 可 stop 的 `QTimer`:hard upper bound(exec 返回后 `stop()`,
              防 pending timeout 在 subloop 被 GC 后触发 use-after-free)
          - `subloop_holder` 在 exec 结束后置 None,done_callback 不再碰已拆毁
            的 QEventLoop。

        任何意外(`RuntimeError` / `BaseException` 含 `CancelledError`)由
        最外层 try/except 兜底 — Qt 的 `closeEvent` 不应让 Python 异常抛回
        主循环,否则会变 "Error calling Python override" 把窗口关成 fail。
        """
        if self._shutdown_cb is not None:
            try:
                import concurrent.futures
                from typing import cast

                from PySide6.QtCore import QEventLoop, QMetaObject, Qt, QTimer
                from PySide6.QtWidgets import QApplication

                # `_shutdown_cb` 类型注解是 `Callable[[], Awaitable[None]]`—
                # Awaitable 严格是 Coroutine 父类,但 run_coroutine_threadsafe
                # 只接受 Coroutine。cast 显式窄化。
                coro = cast(Coroutine[Any, Any, None], self._shutdown_cb())
                fut: concurrent.futures.Future[None] = asyncio.run_coroutine_threadsafe(
                    coro, self.loop,
                )
                deadline_ms = 10_000

                if QApplication.platformName() == "offscreen":
                    # 测试 / CI 路径:不 pump,直接阻塞等 future。
                    # (见 docstring — offscreen 嵌套 run loop 在 macOS 26 VM
                    # 上有 native segfault,且测试 loop 在独立线程,无需 pump。)
                    try:
                        fut.result(timeout=deadline_ms / 1000)
                    except concurrent.futures.TimeoutError:
                        log.warning(
                            "shutdown timed out after %.1fs; cancelling",
                            deadline_ms / 1000,
                        )
                        fut.cancel()
                    except concurrent.futures.CancelledError:
                        log.warning("shutdown coroutine was cancelled")
                else:
                    # production 路径:同线程 qasync loop,嵌套 QEventLoop pump。
                    subloop = QEventLoop()
                    # holder 在 exec 期间持有 subloop;结束后置 None,让后续
                    # done_callback 不再碰已拆毁的 subloop(防 use-after-free)。
                    subloop_holder: list[QEventLoop | None] = [subloop]

                    def _quit_on_done(
                        _f: concurrent.futures.Future[None],
                    ) -> None:
                        # add_done_callback 在 future 完成的线程上跑 — 即
                        # `self.loop` 所在的 asyncio 线程。`subloop` 是绑定
                        # main thread 的本地 QObject,跨线程 quit 必须用
                        # invokeMethod(QueuedConnection) 派到 main thread。
                        sl = subloop_holder[0]
                        if sl is not None:
                            QMetaObject.invokeMethod(
                                sl, "quit", Qt.ConnectionType.QueuedConnection
                            )

                    fut.add_done_callback(_quit_on_done)
                    # hard upper bound:可 stop 的 QTimer,exec 返回后 `stop()`。
                    # `QTimer.singleShot` 静态版在 subloop 被 GC 后到期会调已
                    # 销毁 QObject 的 quit,是 use-after-free。它和 fut callback
                    # 都调 quit(),去重幂等(QEventLoop.quit 可多次调,只置 flag)。
                    deadline = QTimer()
                    deadline.setSingleShot(True)
                    deadline.timeout.connect(subloop.quit)
                    deadline.start(deadline_ms)
                    subloop.exec()
                    # subloop 已退出(正常完成 / cancel / 超时三路)。立刻 stop
                    # pending deadline 并释放 holder,关掉所有指向 subloop 的
                    # 延迟引用。
                    deadline.stop()
                    subloop_holder[0] = None
                    if not fut.done():
                        # deadline 到期触发退出而 future 还没完。主动 cancel
                        # 兜底,避免 task 在 loop 线程残留(loop 关闭时留警告)。
                        log.warning(
                            "shutdown timed out after %.1fs; cancelling",
                            deadline_ms / 1000,
                        )
                        fut.cancel()
                # 收尾:fut 已完成(含 cancelled,concurrent.futures 里 cancelled
                # future 的 done() 也是 True),取结果 / log,不抛回 closeEvent。
                if fut.done():
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
                # 最后一道闸:任何意外(包括 CancelledError)都不应让
                # Qt closeEvent 抛回主循环导致 "Error calling Python override"。
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

        # 0: 实时流(MessageView + MessageDetail 横向并排)
        live_page = QWidget()
        live_layout = QHBoxLayout(live_page)
        live_layout.setContentsMargins(0, 0, 0, 0)
        live_layout.setSpacing(0)
        self.live_view = MessageView()
        self.message_detail = MessageDetail()
        live_layout.addWidget(self.live_view, 1)
        live_layout.addWidget(self.message_detail, 0)
        self.stack.addWidget(live_page)

        # 1: 大盘
        self.dashboard = DashboardWidget(self.app, self.monitor, loop=self.loop)
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
        # 常驻右侧的 TG 通信状态(addPermanentWidget 不会被 showMessage 临时消息顶掉)
        self._conn_label = QLabel("TG 未连接")
        self.status_bar.addPermanentWidget(self._conn_label)
        self.status_bar.showMessage("就绪")

        root.addWidget(right, 1)
        self.setCentralWidget(central)

        # ---- 信号连接 ----
        self.nav.current_changed.connect(self.stack.setCurrentIndex)
        self.header.btn_logout.clicked.connect(self._on_logout_clicked)
        self.header.btn_action.clicked.connect(self._on_header_action)
        self.header.search_bar.text_changed.connect(self._on_search_changed)
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

    def _wire_shortcuts(self) -> None:
        """全局键盘快捷键。

          Ctrl+1/2/3/4 — 切换 tab
          Ctrl+R      — 刷新频道列表
          Ctrl+F      — 聚焦搜索框
          Ctrl+E      — 导出
          Ctrl+T      — 切换主题
        """
        from PySide6.QtGui import QKeySequence, QShortcut

        for idx in range(4):
            sc = QShortcut(QKeySequence(f"Ctrl+{idx + 1}"), self)
            sc.activated.connect(lambda i=idx: self._switch_tab(i))

        sc_refresh = QShortcut(QKeySequence("Ctrl+R"), self)
        sc_refresh.activated.connect(self._on_refresh_channels)

        sc_search = QShortcut(QKeySequence("Ctrl+F"), self)
        sc_search.activated.connect(self._focus_search)

        sc_export = QShortcut(QKeySequence("Ctrl+E"), self)
        sc_export.activated.connect(self._on_export)

        sc_theme = QShortcut(QKeySequence("Ctrl+T"), self)
        sc_theme.activated.connect(self._on_theme_toggle)

    def _switch_tab(self, idx: int) -> None:
        self.nav.set_current(idx)
        # nav.set_current 已经 emit current_changed,stack 会自动跟

    def _focus_search(self) -> None:
        """聚焦到搜索框 + 自动切到 LIVE 页(搜索只在消息视图里有意义)"""
        self._switch_tab(0)
        self.header.search_bar.edit.setFocus()
        self.header.search_bar.edit.selectAll()

    def _on_theme_toggle(self) -> None:
        """切换浅色/暗色主题。"""
        from tgmonitor.ui.theme import ThemeManager

        new = ThemeManager.toggle()
        # 更新主题按钮图标
        self.header.btn_theme.setText("☀" if new.value == "dark" else "🌙")
        # 刷新 nav bar 内部样式
        self.nav.refresh_theme()
        # 频道类型图标(已 tinted)需要按新主题重画
        if hasattr(self.channel_panel, "refresh_theme"):
            self.channel_panel.refresh_theme()
        self.status_bar.showMessage(
            f"已切换到 {'暗色' if new.value == 'dark' else '浅色'}主题", 2000,
        )

    # ======================== ViewModel 事件绑定 ========================

    def _wire_events(self) -> None:
        self._vm.message_received.connect(self._on_message_received)
        self._vm.media_downloaded.connect(self._on_media_downloaded)
        self._vm.login_state.connect(self._on_login_state)
        self._vm.conn_state.connect(self._on_conn_state)
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
        run_coro(self.loop, self.app.client.logout(), error_label="logout")

    def _on_search_changed(self, txt: str) -> None:
        """搜索框内容变化 → 透传给 LIVE view 的 MessageView 过滤。"""
        self.live_view.set_filter(txt)

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

    def _on_media_downloaded(self, e) -> None:
        """媒体下载结束(成功/失败) → 实时流行与详情面板刷新状态。"""
        if not isinstance(e, MediaDownloaded) or e.media is None:
            return
        self.live_view.update_media_status(
            e.channel_id, e.telegram_msg_id, e.media,
        )
        self.message_detail.refresh_if_showing(e.channel_id, e.telegram_msg_id)

    def _on_login_state(self, state: str) -> None:
        self.status_bar.showMessage(f"登录状态: {state}", 4000)

    def _on_conn_state(self, state: str) -> None:
        text = {
            "waiting_for_network": "TG 等待网络",
            "connecting": "TG 连接中…",
            "updating": "TG 同步中…",
            "ready": "TG 已连接",
            "unknown": "TG 状态未知",
        }.get(state, f"TG {state}")
        self._conn_label.setText(text)

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
        """同步请求入口 — 编排 4 个 helper 把 38 行展开成 10 行 orchestrator。

        流程:
          1. titles dict 从 VM known_channels 拉(channel id → title)
          2. show options dialog → user 取消或 settings 不合规就 None
          3. open progress dialog + connect VM signals to it
          4. run_coro 后台 fire 同步 — 协程 finally 里清理 signal connections
        """
        titles = self._build_sync_titles(channel_ids)
        options = self._show_sync_options_dialog(channel_ids, titles)
        if options is None:
            return

        progress_dlg = SyncProgressDialog(
            titles,
            cancel_cb=self.app.channel_sync.cancel,
            parent=self,
        )
        self._wire_sync_progress_signals(progress_dlg)

        async def _go() -> None:
            try:
                await self.app.sync_channels(channel_ids, options)
            finally:
                # progress dialog 关闭后 disconnect VM signals — 防 dangling slot
                # Qt C++ 侧 reference 累积。dialog 已 GC / signal 已断 → swallow
                # RuntimeError / TypeError(Qt 已 disconnect 后再调 disconnect 抛)
                try:
                    self._vm.sync_progress.disconnect(progress_dlg.on_progress)
                    self._vm.sync_done.disconnect(progress_dlg.on_done)
                except (RuntimeError, TypeError):
                    pass

        run_coro(self.loop, _go(), error_label="sync_with_progress")
        progress_dlg.exec()

    def _build_sync_titles(self, channel_ids: list[int]) -> dict[int, str]:
        """`channel_id → title` 给 SyncOptionsDialog / SyncProgressDialog 显示用。

        VM 里有 DTO 用 `.title`,没找到(VMe 还没 bootstrap)回退 `#<id>`。
        """
        titles: dict[int, str] = {}
        for cid in channel_ids:
            ch = self._vm.known_channels.get(cid)
            titles[cid] = ch.title if ch else f"#{cid}"
        return titles

    def _show_sync_options_dialog(
        self,
        channel_ids: list[int],
        titles: dict[int, str],
    ) -> SyncOptions | None:
        """弹 SyncOptionsDialog + 读取 user 选择的选项。

        返回 None:用户取消 OR 校验失败(让 options() 返 None)— 调用方不继续。
        """
        defaults = SyncOptions(
            chat_delay_ms=self.app.settings.sync_chat_delay_ms,
            page_delay_ms=self.app.settings.sync_page_delay_ms,
            resume_from_saved=self.app.settings.sync_resume_from_saved,
        )
        dlg = SyncOptionsDialog(channel_ids, titles, defaults, self)
        if not dlg.exec():
            return None
        return dlg.options()

    def _wire_sync_progress_signals(self, dialog: SyncProgressDialog) -> None:
        """把 VM 的 sync_progress / sync_done Signal 连到 progress dialog 的 slot。

        解绑在 `_on_sync_requested._go()` 的 `finally` 里做(避免 dialog 已
        destroy 时 disconnect 抛 RuntimeError 的处理)— 见 REVIEW M3 关于
        dialog GC 后 dangling slot 的提示。
        """
        self._vm.sync_progress.connect(dialog.on_progress)
        self._vm.sync_done.connect(dialog.on_done)

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
    """顶部紧凑信息栏:左标题 + 搜索 + 右登录状态 + 操作。

    不再用 QToolBar,改为自定义 widget,视觉更紧凑。
    """

    # 类变量无 None 占位 — `__init__` 内必建 `btn_logout/btn_action/btn_theme/search_bar`,
    # mypy 看到实例属性 = QPushButton / SearchBar 而非 X | None,清掉 21 处 union-attr。
    # 不建 `_HeaderBar()` 之外的实例路径,删占位安全。

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("headerBar")
        self.setFixedHeight(44)

        hbox = QHBoxLayout(self)
        hbox.setContentsMargins(16, 0, 16, 0)
        hbox.setSpacing(12)

        # 左: 标题
        title = QLabel("tgmonitor")
        title.setObjectName("appTitle")
        hbox.addWidget(title)

        # 搜索条
        self.search_bar = SearchBar()
        hbox.addWidget(self.search_bar)
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

        # 主题切换按钮 — 显示「当前切到该主题后会变成什么」
        from tgmonitor.ui.theme import ThemeManager
        cur = ThemeManager.current()
        self.btn_theme = QPushButton("🌙" if cur.value == "light" else "☀")
        self.btn_theme.setObjectName("headerActionBtn")
        self.btn_theme.setFixedWidth(36)
        self.btn_theme.setToolTip("切换主题(Ctrl+T)")
        hbox.addWidget(self.btn_theme)

    def update_state(self, state: str, detail: str = "") -> None:
        dot = state_dot(state)
        label = state_label(state)
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
