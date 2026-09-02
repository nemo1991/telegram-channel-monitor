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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Coroutine, cast

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QCloseEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
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
    MessageDeleted,
    NotificationRequested,
    QuitRequested,
)
from tgmonitor.ui._async import run_coro
from tgmonitor.ui.nav_bar import VerticalNavBar
from tgmonitor.ui.state_labels import state_dot, state_label
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel
from tgmonitor.ui.widgets.channel_widget import ChannelWidget
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget
from tgmonitor.ui.widgets.export_dialog import ExportDialog
from tgmonitor.ui.widgets.export_progress_dialog import ExportProgressDialog
from tgmonitor.ui.widgets.lightbox_dialog import LightboxDialog
from tgmonitor.ui.widgets.media_manager_widget import MediaManagerWidget
from tgmonitor.ui.widgets.message_detail import MessageDetail
from tgmonitor.ui.widgets.message_view import MessageView
from tgmonitor.ui.widgets.search_bar import SearchBar
from tgmonitor.ui.widgets.settings_page import SettingsPage
from tgmonitor.ui.widgets.sync_dialog import (
    SyncOptionsDialog,
    SyncProgressDialog,
)
from tgmonitor.ui.widgets.tray_icon import TrayIcon

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)

ShutdownCb = Callable[[], Awaitable[None]]

# ---- 状态映射 ----


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
        self.app = app
        self.monitor = monitor
        self.loop = loop
        self._objects_error = objects_error
        self._objects_warn_label: QLabel | None = None
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

        self.env_path = env_path or (_user_data_dir() / ".env")
        self.setWindowTitle("tgmonitor · Telegram 频道监听")
        self.resize(1180, 740)

        self._vm = MonitorViewModel(app, monitor, loop)
        self._shutdown_cb: ShutdownCb | None = None
        # 2026-08-30 v1.5.0 PR #A4:TrayIcon 实例。`is_active=False` 时
        # (offscreen / Linux 无 indicator)closeEvent 不进入 minimize-to-tray。
        self._tray: TrayIcon | None = None
        # 2026-08-30 PR #A4:tray「退出」→ VM 收到 QuitRequested → emit
        # `quit_requested` signal → 此 handler → 真退出 qt_app.quit。
        self._vm.quit_requested.connect(self._on_vm_quit_requested)
        self._build_ui()
        self._build_menu()
        self._build_tray()
        self._wire_shortcuts()
        self._wire_events()
        self._wire_theme_change_signal()
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
        # 2026-08-30 v1.5.0 PR #A4:首次 close(用户点 X / Alt+F4)默认
        # 最小化到 tray,不退出。File→Quit / tray「退出」菜单走
        # `qt_app.quit()` → `aboutToQuit` → 关闭全部窗口(到这一步时
        # `_truly_quit=True`),这里才走 shutdown。
        if not self._truly_quit and self._tray is not None and self._tray.is_active:
            self.hide()
            if not self._tray_first_close_hint_shown:
                self._tray_first_close_hint_shown = True
                # 系统通知(若有 tray)+ 状态栏永久提示
                self.app.bus.publish_threadsafe(
                    self.loop,
                    NotificationRequested(
                        level="info",
                        title="tgmonitor 已在后台运行",
                        body="右键托盘图标可恢复窗口或退出应用",
                        click_action="show_main",
                    ),
                )
                self.statusBar().showMessage(
                    "已在后台运行 · 右键托盘图标或 File 菜单恢复",
                    8000,
                )
            event.ignore()
            return
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
                    coro,
                    self.loop,
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
                            QMetaObject.invokeMethod(sl, "quit", Qt.ConnectionType.QueuedConnection)

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
                        log.warning("shutdown raised: %s: %s", type(exc).__name__, exc)
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

        # 状态栏
        self.setStatusBar(QStatusBar())
        self.status_bar = self.statusBar()
        # 常驻右侧的 TG 通信状态(addPermanentWidget 不会被 showMessage 临时消息顶掉)
        self._conn_label = QLabel("TG 未连接")
        self.status_bar.addPermanentWidget(self._conn_label)
        # v1.0.22:启动时对象存储 connect 失败 → 状态栏红字常驻提示(不只写日志)。
        # 用户从日志看不到问题,媒体下载又静默失败,必须让「对象存储不可用」在
        # UI 上直接可见;设置页热重载成功(`_on_settings_changed`)后自动移除。
        if self._objects_error:
            self._objects_warn_label = QLabel(f"⚠ 对象存储不可用: {self._objects_error}")
            self._objects_warn_label.setStyleSheet("color: #d03030; font-weight: 600;")
            self._objects_warn_label.setToolTip(
                "媒体文件将无法下载 / 保存。请到 设置 → 对象存储 检查配置"
                "(S3/MinIO 填 API 地址,勿填控制台地址)后重新保存。"
            )
            self.status_bar.addPermanentWidget(self._objects_warn_label)
        self.status_bar.showMessage("就绪")

        root.addWidget(right, 1)
        self.setCentralWidget(central)

        # ---- 信号连接 ----
        self.nav.current_changed.connect(self.stack.setCurrentIndex)
        # Media Manager 页切换时自动拉一次列表(VM 异步,首次可能空 → 等频道就绪)
        self.nav.current_changed.connect(self._on_nav_changed)
        self.header.btn_logout.clicked.connect(self._on_logout_clicked)
        self.header.btn_action.clicked.connect(self._on_header_action)
        self.header.search_bar.text_changed.connect(self._on_search_changed)
        # 2026-09-02 v1.5.2 PR #B5:date_from/to 折叠面板变化 → 立即拉
        # (不需要 debounce — 用户主动调日历 = 期望立刻响应)
        self.header.search_bar.date_changed.connect(self._on_search_date_changed)
        self.header.btn_theme.clicked.connect(self._on_theme_toggle)

        # 2026-09-02 v1.5.2 PR #B5:300ms debounce — 文本输入 300ms 内无新
        # 字符才触发 vm.search_messages。parent=self 确保 MainWindow 关闭
        # 时 timer deleteLater 自然清理,无悬挂。
        from PySide6.QtCore import QTimer

        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(300)
        self._search_debounce.timeout.connect(self._on_search_debounce_fire)

        # Dashboard 快速操作
        self.dashboard.on_refresh = self._on_refresh_channels
        self.dashboard.on_export = self._on_export
        self.dashboard.on_sync_all = self._on_sync_all_channels

        # ChannelWidget 信号
        self.channel_panel.btn_refresh.clicked.connect(self._on_refresh_channels)
        self.channel_panel.sync_requested.connect(self._on_sync_requested)

        # MediaManagerWidget 信号
        self.media_manager.refresh_requested.connect(lambda: self._on_media_refresh())
        self.media_manager.open_requested.connect(
            lambda cid, mid, idx: self._vm.open_media(cid, mid, idx)
        )
        # 2026-08-27 v1.4.0 PR #16:Reveal / Copy 按钮 wire。
        self.media_manager.reveal_requested.connect(self._on_media_reveal)
        self.media_manager.copy_requested.connect(self._on_media_copy)
        self.media_manager.retry_requested.connect(
            lambda cid, mid, idx: self._vm.retry_media(cid, mid, idx)
        )
        self.media_manager.delete_requested.connect(
            lambda cid, mid, idx: self._vm.delete_media(cid, mid, idx)
        )
        self.media_manager.batch_retry_requested.connect(self._on_media_batch_retry)
        self.media_manager.batch_delete_requested.connect(self._on_media_batch_delete)
        self.media_manager.prune_requested.connect(self._on_media_prune)
        # 2026-08-25 PR #4:按频道批量删除 — 二次确认后调 vm.delete_by_channel
        self.media_manager.clear_channel_requested.connect(self._on_media_clear_channel)
        # 2026-08-25 v1.3.0 PR #7:Media Manager 当前视图 → CSV 一键导出
        self.media_manager.export_csv_requested.connect(self._on_media_export_csv)
        # 2026-09-01 v1.5.1 PR #B4:ZIP 打包导出 — 镜像 CSV 流程,接收
        # `(out_path, include_thumbnails)`,构造 ExportRequest(format=ZIP)。
        self.media_manager.export_zip_requested.connect(self._on_media_export_zip)
        # 2026-08-31 v1.5.0 PR #A8:Lightbox 内嵌预览 — 点缩略图 → 异步加载
        # 原图 bytes → QPixmap → 弹 LightboxDialog。
        self.media_manager.preview_requested.connect(self._on_media_preview)
        # 2026-08-25 v1.3.0 PR #5:打开媒体失败 → QMessageBox.warning 带原因
        self._vm.open_media_failed.connect(self._on_open_media_failed)

        # MessageView → MessageDetail(点击消息显示详情)
        self.live_view.message_selected.connect(self.message_detail.show_message)
        # 2026-08-31 v1.5.0 PR #A8:MessageDetail 媒体卡 click → Lightbox(与
        # media_manager.preview_requested 走同一个加载器)
        self.message_detail.preview_requested.connect(self._on_media_preview)

    def _build_menu(self) -> None:
        """File menu — 「显示主窗口 / 暂停监听 / 退出」(2026-08-30 v1.5.0 PR #A4)。

        「暂停监听」对应 tray menu 同名动作 — 留作 v1.5.1,本 PR 行为同
        tray 一致(发 QuitRequested(pause=True),目前无订阅者,只 log)。
        「退出」走 `qt_app.quit()` → aboutToQuit 触发 setQuitOnLastWindowClosed
        之前的 `window.close()`,此时 `_truly_quit=True` 已置,closeEvent
        不会再 minimize-to-tray,直接走 shutdown。
        """
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("&File")
        act_show = QAction("显示主窗口", self)
        act_show.setShortcut("Ctrl+0")
        act_show.triggered.connect(self._show_and_raise)
        file_menu.addAction(act_show)
        file_menu.addSeparator()
        act_pause = QAction("暂停监听", self)
        act_pause.triggered.connect(
            lambda: self.app.bus.publish_threadsafe(self.loop, QuitRequested(pause=True))
        )
        file_menu.addAction(act_pause)
        act_quit = QAction("退出", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self._quit_app)
        file_menu.addAction(act_quit)

    def _build_tray(self) -> None:
        """系统托盘图标(2026-08-30 v1.5.0 PR #A4)。

        `isSystemTrayAvailable()` 假时(offscreen / Linux 无 indicator)— `_tray`
        仍创建(订阅 handler 内部 no-op),但 `is_active=False`,closeEvent
        不进入 minimize-to-tray 分支,直接走 shutdown。
        """
        self._tray = TrayIcon(self, self.app)
        if self._tray.is_active:
            self._tray.show()
        # 订阅 NotificationRequested → 状态栏 fallback(无 tray 系统也
        # 走通)。TrayIcon 内部已订阅一次,这里 main_window 再订阅一份做
        # status bar 兜底,EventBus 广播 N 订阅者互不影响。
        self.app.bus.subscribe(NotificationRequested, self._on_notification_fallback)

    def _show_and_raise(self) -> None:
        """File / tray「显示主窗口」共用 — unminimize + 顶置 + 焦点。"""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_app(self) -> None:
        """File→Quit / Ctrl+Q — 标 `_truly_quit=True` 后调 `qt_app.quit()`。

        路径:`qt_app.quit()` → Qt 主循环结束 → 各 QWindow 关 → closeEvent
        触发 → `_truly_quit=True` → 直走 shutdown → 真退出。
        """
        self._truly_quit = True
        # QApplication.instance() 静态返回 QCoreApplication | None;
        # `_quit_app` 只在用户主动触发(File→Quit / Ctrl+Q / tray「退出」)
        # 调起,此时 QApplication 必 alive(否则整个 UI 早关了)— assert 兜底。
        qt_app = QApplication.instance()
        assert qt_app is not None, "QApplication gone before quit"
        qt_app.quit()

    def _on_vm_quit_requested(self) -> None:
        """2026-08-30 v1.5.0 PR #A4:tray menu「退出」→ VM 转发 → 真退出。

        与 `_quit_app` 同义,但走 tray「退出」(不绕开 Qt 主循环)路径:
        VM `quit_requested` signal → 此 slot → `qt_app.quit`。
        """
        self._quit_app()

    async def _on_notification_fallback(self, event: object) -> None:
        """无 tray 系统(Linux 无 indicator / offscreen)→ 状态栏 fallback。

        `isinstance` 严格过滤:EventBus publish 任意事件都会触发此 handler
        (因 subscribe 接的是 type object,会按 MRO 匹配),此 handler 只关心
        NotificationRequested。
        """
        if not isinstance(event, NotificationRequested):
            return
        # 系统通知已由 TrayIcon 发出;此处只走状态栏(覆盖两种环境)
        if self._tray is None or not self._tray.is_active:
            self.statusBar().showMessage(f"{event.title}: {event.body}", 5000)

    def _wire_shortcuts(self) -> None:
        """全局键盘快捷键。

        Ctrl+1/2/3/4/5 — 切换 tab(LIVE/DASHBOARD/CHANNELS/MEDIA/SETTINGS)
        Ctrl+R      — 刷新频道列表
        Ctrl+F      — 聚焦搜索框
        Ctrl+E      — 导出
        Ctrl+T      — 切换主题
        """
        from PySide6.QtGui import QKeySequence, QShortcut

        for idx in range(5):
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

        # 2026-08-30 v1.5.0 PR #A5:补齐快捷键(plan 列了 ~10)
        # - Ctrl+Q 真退出(同 File→Quit)— 兜底 macOS cmd+Q 不发到 Qt 窗口
        # - Ctrl+, 打开设置页(STTINGS tab index=4)
        # - Esc 全局关闭 — 取消搜索框 focus / 关 dialog
        # - Up/Down LIVE 流上下条(QListWidget 原生已支持,这里显式重绑
        #   是为了 detail panel 已 focus 时也能跳行 — Qt shortcut context
        #   = Window,任意子 widget focus 都触发)
        # - Ctrl+C 复制当前消息 text 到剪贴板(只在 LIVE)
        sc_quit = QShortcut(QKeySequence("Ctrl+Q"), self)
        sc_quit.activated.connect(self._quit_app)
        sc_settings = QShortcut(QKeySequence("Ctrl+,"), self)
        sc_settings.activated.connect(lambda: self._switch_tab(4))
        sc_esc = QShortcut(QKeySequence("Esc"), self)
        sc_esc.activated.connect(self._on_global_escape)
        sc_copy = QShortcut(QKeySequence("Ctrl+C"), self)
        sc_copy.activated.connect(self._copy_current_message_text)

    def _switch_tab(self, idx: int) -> None:
        self.nav.set_current(idx)
        # nav.set_current 已经 emit current_changed,stack 会自动跟

    def _on_nav_changed(self, idx: int) -> None:
        """nav tab 切换 — 切到 MEDIA 页(3)触发一次列表刷新。

        其他页忽略。VM 异步拉数据,第一次可能因 known_channels 还没就绪而
        短暂为空,后续 channels_changed → _refresh_state 也会再刷一次。
        """
        if idx == 3:
            self._on_media_refresh()

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
            f"已切换到 {'暗色' if new.value == 'dark' else '浅色'}主题",
            2000,
        )

    def _wire_theme_change_signal(self) -> None:
        """2026-08-30 v1.5.0 PR #A5:ThemeManager.theme_changed → nav_bar 重画。

        监听 ThemeManager 的全局 signal(SYSTEM 态 OS 切色时
        `_on_system_scheme_changed` 也会 emit)— nav bar / channel panel
        按新主题重 tint,免得 OS 切色后图标颜色 stale。
        """
        from tgmonitor.ui.theme import ThemeManager

        ThemeManager._instance().theme_changed.connect(self._on_theme_changed)

    def _on_theme_changed(self) -> None:
        """2026-08-30 v1.5.0 PR #A5:ThemeManager 主题变 → UI 同步。"""
        from tgmonitor.ui.theme import ThemeManager

        actual = ThemeManager.actual()
        # 按钮图标按 actual(非 current)—— SYSTEM 态下按 OS 实际值显示
        self.header.btn_theme.setText("☀" if actual.value == "dark" else "🌙")
        self.nav.refresh_theme()
        if hasattr(self.channel_panel, "refresh_theme"):
            self.channel_panel.refresh_theme()

    def _on_global_escape(self) -> None:
        """2026-08-30 v1.5.0 PR #A5:Esc 全局快捷键。

        优先级:
        1. 搜索框有 focus → 清空搜索 + 失焦(回 LIVE)
        2. 设置页 / 子 dialog 有 focus → 退到默认(LIVE)
        3. 否则 no-op(避免误清 LIVE 选中)

        2026-09-02 v1.5.2 PR #B5:搜索框 clear 同时清 date panel + 折叠按钮 +
        stop debounce timer(避免 stale fetch 跑到空 query 上)。
        """
        sb = self.header.search_bar.edit
        if sb.hasFocus():
            # 2026-09-02 PR #B5:stop timer 防 stale search 在 clear 后继续
            # 触发(用户在 300ms 内按 Esc 但 search 已 emit 过);直接 clear()
            # 会走 SearchBar._on_text_changed → 启新 timer,这里提前 stop。
            self._search_debounce.stop()
            self.header.search_bar.clear()  # SearchBar.clear() 重置 text + dates + adv
            sb.clearFocus()
            # 主动 emit `[]` → 清空视图(后续 live 消息自然 append 重建 LIVE 流)
            self.live_view.set_messages([])
            return
        # 若当前不在 LIVE,回 LIVE(简化 — 用户感知的「取消」)
        if self.stack.currentIndex() != 0:
            self._switch_tab(0)

    def _copy_current_message_text(self) -> None:
        """2026-08-30 v1.5.0 PR #A5:Ctrl+C 复制当前 LIVE 选中消息 text。

        MessageView 是 QListWidget,QListWidgetItem.data(Qt.UserRole)
        存 MessageDTO(v1.4.0 PR #3 实装)— 取 text 字段放剪贴板。
        """
        if self.stack.currentIndex() != 0:
            return  # 只在 LIVE 页生效
        item = self.live_view.currentItem()
        if item is None:
            return
        # data(Qt.UserRole) 存的是 MessageDTO
        from PySide6.QtCore import Qt as QtNS

        msg = item.data(QtNS.UserRole)
        text = getattr(msg, "text", None) or ""
        if not text:
            self.statusBar().showMessage("当前消息无文本", 1500)
            return
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)
        self.statusBar().showMessage(f"已复制 {len(text)} 字", 1500)

    # ======================== ViewModel 事件绑定 ========================

    def _wire_events(self) -> None:
        self._vm.message_received.connect(self._on_message_received)
        self._vm.message_edited.connect(self._on_message_edited)
        self._vm.media_downloaded.connect(self._on_media_downloaded)
        # 2026-09-01 v1.5.1 PR #B3:下载进度反馈 — VM signal → media_manager
        # 直接刷新对应 row 的「已下载 X / Y (Z%)」文字。
        self._vm.media_download_progress.connect(self.media_manager.on_download_progress)
        self._vm.login_state.connect(self._on_login_state)
        self._vm.conn_state.connect(self._on_conn_state)
        self._vm.channels_changed.connect(self._refresh_state)
        self._vm.export_done.connect(self._on_export_done)
        self._vm.error.connect(self._on_error)
        self._vm.settings_changed.connect(self._on_settings_changed)
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

        # 订阅 EventBus 登录状态变化(状态点更新)
        self.app.bus.subscribe(LoginStateChanged, self._on_bus_login)
        self.app.bus.subscribe(AuthErrorOccurred, self._on_bus_auth_error)
        # MessageDeleted 直接订阅总线(VM 没 Qt signal — LIVE 流需要按事件
        # 删行)
        self.app.bus.subscribe(MessageDeleted, self._on_bus_message_deleted)

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
            req = dlg.request()
            # 2026-08-30 v1.5.0 PR #A3:导出参数敲定后弹进度对话框 +
            # 后台 start_export。dialog 自身订阅 vm.export_progress +
            # 完成后由 _on_export_done 关闭。
            self._export_dialog = ExportProgressDialog(self._vm, parent=self)
            self._export_dialog.show()
            self._vm.start_export(req)

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
        """2026-09-02 v1.5.2 PR #B5:搜索框变化 → 双过滤。

        1) 即时:`live_view.set_filter(txt)` 内存快过滤已加载 LIVE 流(无 IO,
        UI 立即响应 — 当前 200 条 LIVE 的 hide/show)。
        2) 300ms debounce:启动 `_search_debounce` QTimer;到点触发
           `vm.search_messages(...)` → 异步拉 storage 服务端命中 →
           emit `message_search_results` → `live_view.set_messages(...)`
           批量替换视图。

        双过滤不冲突:快过滤是「视觉欺骗」(本地 hide/show),set_messages 是
        「权威」(server-side 命中替换)。
        """
        # 1) 即时快过滤(无 IO)
        self.live_view.set_filter(txt)
        # 2) debounce 启动 timer;每次输入重置,只有 300ms 内没新输入才触发
        self._search_debounce.start()

    def _on_search_date_changed(self, dt_from, dt_to) -> None:
        """2026-09-02 v1.5.2 PR #B5:日期范围变化 → 走同一 debounce 拉结果。"""
        # 日期变化立即触发(不需要 300ms 等待 — 用户主动调整日历 = 期望立刻响应)
        self._run_search_query()

    def _on_search_debounce_fire(self) -> None:
        """300ms debounce 到点 → 调 vm.search_messages。"""
        self._run_search_query()

    def _run_search_query(self) -> None:
        """拉一次搜索结果:空 query → emit `[]` 清空视图(回 LIVE 流占位)。

        `live_view.set_messages([])` 行为是 clear_view — 但 LIVE 流真正
        恢复需要重新 `vm.load_recent_messages()` 重新拉 200 条。这里简化为
        「空 query = 清空列表」(用户清空搜索时视觉上是「无结果」,后续如有
        新 LIVE 消息到达,append 会加进空表 — 等于 LIVE 流以增量形式
        重建)。如果未来需要"清空 = 重拉 LIVE 200 条",改成 emit 触发
        `vm.load_recent_messages()` 即可。
        """
        sb = self.header.search_bar
        txt = sb.text()
        df, dt = sb.date_range()
        if not txt and df is None and dt is None:
            # 空 query → 清空视图(后续 live 消息 append 进去会自然重建 LIVE 流)
            self.live_view.set_messages([])
            return
        # 走已订阅频道 id 列表(从 known_channels 拿 — VM 已维护)
        self._vm.search_messages(
            text=txt,
            date_from=df,
            date_to=dt,
            limit=200,
            channel_ids=list(self._vm.known_channels.keys()),
        )

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

    def _on_message_edited(self, m: MessageDTO) -> None:
        """2026-08-24:TDLib updateMessageContent → MessageEdited 事件 → 整条 cell 重渲。

        按 (channel_id, telegram_msg_id) 找现有 row,不增删。
        """
        self.live_view.replace_message(m)

    def _on_media_downloaded(self, e) -> None:
        """媒体下载结束(成功/失败) → 实时流行与详情面板刷新状态。"""
        if not isinstance(e, MediaDownloaded) or e.media is None:
            return
        self.live_view.update_media_status(
            e.channel_id,
            e.telegram_msg_id,
            e.media,
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
        # 2026-08-30 v1.5.0 PR #A3:关闭进度对话框(如有)— dialog 自身
        # 已解 signal 连接,accept() 安全
        dlg = getattr(self, "_export_dialog", None)
        if dlg is not None:
            dlg.accept()
            # del 而非 = None:避免 ExportProgressDialog | None 注解变化
            # 蔓延全文件;此字段本来就只在 export 期间有值
            del self._export_dialog
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
        self,
        what: str,
        needs_relogin: bool,
        needs_restart: bool,
        backend_label: str,
    ) -> None:
        # v1.0.22:热重载成功即代表对象存储本轮已通过无条件 connect 校验,
        # 启动时挂上的红字警告可移除
        if self._objects_warn_label is not None:
            self.status_bar.removeWidget(self._objects_warn_label)
            self._objects_warn_label.deleteLater()
            self._objects_warn_label = None
        msg = f"已热重载: {what} → {backend_label}"
        self.status_bar.showMessage(msg, 5000)
        if needs_relogin:
            QMessageBox.information(
                self,
                "凭据已变更",
                "Telegram 凭据已变更。\n请重新登录以继续监听。",
            )
        elif needs_restart:
            # v1.0.23:proxy / session_dir 是 TdlibClient 构造参数,运行时
            # 不重建 client,变更已写入 .env 但需重启应用才生效
            QMessageBox.information(
                self,
                "需重启生效",
                "代理或会话目录已变更并保存。\nTDLib 客户端在启动时创建,请重启应用使其生效。",
            )

    # ======================== Media Manager 槽 (2026-08-24) ========================

    def _on_media_refresh(self) -> None:
        """Media Manager 顶部 refresh / filter 变化 → 拉一次列表。

        同时把当前 known channels 推给 widget 的 channel 下拉框。
        """
        self.media_manager.set_known_channels(list(self._vm.known_channels.values()))
        f = self.media_manager.current_filters()
        self._vm.load_media_list(
            channel_id=f["channel_id"],
            status=f["status"],
            media_type=f["media_type"],
            search=f["search"],
        )

    def _on_media_batch_retry(self, keys: list) -> None:
        """批量 retry — keys 是 widget 内部的 _RowKey 列表(对象)。"""
        items = [(k.channel_id, k.telegram_msg_id, k.media_idx) for k in keys]

        async def _go() -> None:
            for cid, mid, idx in items:
                try:
                    await self.app.retry_media(cid, mid, idx)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "batch retry failed: %s/%s/%d",
                        cid,
                        mid,
                        idx,
                    )

        run_coro(self.loop, _go(), error_label="batch_retry_media")

    def _on_media_batch_delete(self, keys: list) -> None:
        """批量 delete — 二次确认 + 调 VM.delete_media_batch。"""
        if not keys:
            return
        n = len(keys)
        ans = QMessageBox.warning(
            self,
            "删除确认",
            f"确定删除选中的 {n} 条媒体?\n"
            "删除后无法撤销;若 bytes 不再被引用,文件也会从对象存储删除。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        items = [(k.channel_id, k.telegram_msg_id, k.media_idx) for k in keys]
        self._vm.delete_media_batch(items)
        # 删除完顺手刷新一次列表
        self._on_media_refresh()

    def _on_media_prune(self) -> None:
        """Media Manager 「Prune Orphans」按钮 — 二次确认 → reconcile(dry_run=False)。

        dry_run=False 会真删 ObjectStore bytes;UI 强警告。
        """
        ans = QMessageBox.warning(
            self,
            "Prune Orphans",
            "扫描对象存储并删除无引用的孤立文件。\n此操作不可撤销,确认执行?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        self._vm.reconcile_orphans(dry_run=False)

    def _on_media_clear_channel(self, channel_id: int) -> None:
        """2026-08-25 v1.3.0 PR #8:Media Manager 「Clear Channel」按钮 —
        先 fire `vm.preview_delete_by_channel(channel_id)`,VM 后台读 storage
        后 emit `delete_preview_ready(DeleteChannelPreview)`,MainWindow
        接到后弹 `ClearChannelPreviewDialog`(必勾 ack 才 enable OK)。
        用户 OK 才走 `vm.delete_by_channel`;Cancel 不动。
        """
        self._vm.delete_preview_ready.connect(
            lambda preview, cid=channel_id: self._show_clear_channel_dialog(cid, preview),
        )
        self._vm.preview_delete_by_channel(channel_id)

    def _show_clear_channel_dialog(
        self,
        channel_id: int,
        preview: object,
    ) -> None:
        """`delete_preview_ready` 信号 handler — 弹 dialog,Accepted 才真删。"""
        from tgmonitor.core.dto import DeleteChannelPreview
        from tgmonitor.ui.widgets.clear_channel_preview_dialog import (
            ClearChannelPreviewDialog,
        )

        # 断开一次性连接,避免重复触发
        try:
            self._vm.delete_preview_ready.disconnect()
        except (RuntimeError, TypeError):
            pass  # 没连过 / 已断

        if not isinstance(preview, DeleteChannelPreview):
            return
        ch = self._vm.known_channels.get(channel_id)
        title = ch.title if ch else ""
        dlg = ClearChannelPreviewDialog(preview, title, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._vm.delete_by_channel(channel_id)

    def _on_media_export_csv(self, out_path: str) -> None:
        """2026-08-25 v1.3.0 PR #7:Media Manager 「Export CSV」按钮 —
        构造 `MediaExportRequest`(透传 widget 当前 filter / sort / offset),
        调 `vm.export_media_list` 走 ExportService 异步生成器;完成 / 失败
        通过既有 `vm.export_done` 信号在 `_on_export_done` 弹消息框。
        """
        from tgmonitor.core.dto import MediaExportRequest, SortDir, SortKey

        f = self.media_manager.current_filters()
        # `current_filters()` 返回 dict[str, Any];sort/sort_dir 在 storage 层
        # 已限制为 SortKey/Dir,但 f.get 默认 `Any | None` → 显式 cast。
        sort_val = f.get("sort")
        sort_dir_val = f.get("sort_dir")
        req = MediaExportRequest(
            channel_id=f.get("channel_id"),
            status=f.get("status"),
            media_type=f.get("media_type"),
            search=f.get("search", ""),
            sort=cast(SortKey, sort_val) if sort_val is not None else SortKey.DATE,
            sort_dir=cast(SortDir, sort_dir_val) if sort_dir_val is not None else SortDir.DESC,
            out_path=out_path,
        )
        self._vm.export_media_list(req)

    def _on_media_export_zip(self, out_path: str, include_thumbnails: bool) -> None:
        """2026-09-01 v1.5.1 PR #B4:Media Manager 「Export ZIP」按钮 —
        构造 `ExportRequest(format=ExportFormat.ZIP, include_thumbnails=...)`,
        调 `vm.export_zip` 走 ExportService 异步生成器。

        `channel_ids` 取自 widget 当前 filter 的 channel_id(若 All Channels
        走空 list = 全频道)。`single_message_id=None` = 范围导出,与
        CSV 流程保持一致(单条 ZIP 走 message view context menu,后续 PR)。
        """
        from tgmonitor.core.dto import ExportFormat, ExportRequest

        f = self.media_manager.current_filters()
        # 把 `All Channels` (channel_id is None) → 空 list,跟 ExportRequest
        # 语义对齐(空 = 全频道)
        ch = f.get("channel_id")
        channel_ids = [int(ch)] if ch is not None else []
        req = ExportRequest(
            channel_ids=channel_ids,
            format=ExportFormat.ZIP,
            out_path=out_path,
            include_thumbnails=include_thumbnails,
            single_message_id=None,
        )
        self._vm.export_zip(req)

    def _on_open_media_failed(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
        reason: str,
    ) -> None:
        """2026-08-25 v1.3.0 PR #5:VM.open_media 失败 → QMessageBox.warning
        把 reason 显示给用户(v1.2.0 默默吞错,只能去 log 翻)。
        """
        QMessageBox.warning(
            self,
            "打开媒体失败",
            f"无法打开频道 #{channel_id} 消息 #{telegram_msg_id} 第 {media_idx + 1} 个媒体:\n\n{reason}",
        )

    def _on_media_preview(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        """2026-08-31 v1.5.0 PR #A8:Media Manager 缩略图点击 → Lightbox。

        流程:`app.storage.get_message` 找消息 → VM.load_media_bytes 异步读
        原图 bytes → 主线程 QPixmap.loadFromData 渲染 → 弹 LightboxDialog。
        全程 async,UI handler 立即返回不阻塞 event loop。
        """
        from tgmonitor.core.dto import MediaType

        async def _load_and_show() -> None:
            storage = self.app.storage
            if storage is None:
                return
            msg = await storage.get_message(channel_id, telegram_msg_id)
            if msg is None or media_idx >= len(msg.media):
                return
            med = msg.media[media_idx]
            if med.type not in (
                MediaType.PHOTO,
                MediaType.STICKER,
                MediaType.ANIMATION,
            ):
                # 非图片 — 不弹 lightbox,fallback 到系统查看器
                self._vm.open_media(channel_id, telegram_msg_id, media_idx)
                return
            data = await self._vm.load_media_bytes(med)
            if not data:
                QMessageBox.information(
                    self,
                    "Lightbox",
                    f"无法加载预览(可能未下载或 backend 不可用)。\n文件:{med.file_name or '(no name)'}",
                )
                return

            def _show() -> None:
                pix = QPixmap()
                if not pix.loadFromData(data):
                    QMessageBox.warning(self, "Lightbox", "图片解码失败。")
                    return
                dlg = LightboxDialog(
                    pixmaps=[pix],
                    current=-1,
                    title=med.file_name or "",
                )
                dlg.showFullScreen()

            QTimer.singleShot(0, _show)

        run_coro(self.loop, _load_and_show(), error_label="media_preview")

    def _on_media_reveal(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        """2026-08-27 v1.4.0 PR #16:Reveal in Folder — 调 AppService.reveal_in_folder,
        失败弹 QMessageBox.warning(同 Open 失败模式)。
        """

        async def _go():
            return await self._app.reveal_in_folder(
                channel_id,
                telegram_msg_id,
                media_idx,
            )

        def _after(result):
            if result.success:
                return
            QMessageBox.warning(
                self,
                "Reveal 失败",
                f"无法在文件管理器中显示:\n\n{result.error}",
            )

        run_coro(
            self._qloop,
            _go(),
            on_success=lambda r: _after(r),
            error_label="reveal_in_folder",
        )

    def _on_media_copy(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        """2026-08-27 v1.4.0 PR #16:Copy 路径 / URI — 调 AppService.copy_media_path,
        成功写剪贴板,失败弹 QMessageBox.warning。
        """

        async def _go():
            return await self._app.copy_media_path(
                channel_id,
                telegram_msg_id,
                media_idx,
            )

        def _after(result):
            if not result.success:
                QMessageBox.warning(
                    self,
                    "Copy 失败",
                    f"无法复制路径:\n\n{result.error}",
                )
                return
            # 写剪贴板
            QApplication.clipboard().setText(result.copied_value or "")

        run_coro(
            self._qloop,
            _go(),
            on_success=lambda r: _after(r),
            error_label="copy_media_path",
        )

    async def _on_bus_message_deleted(self, e) -> None:
        """EventBus:MonitorService.delete_message 发的 MessageDeleted → LIVE 流删行。

        bytes 清理由 MonitorService.delete_message 内部已做,这里只管 UI。
        """
        if not isinstance(e, MessageDeleted):
            return
        self.live_view.remove_row(e.channel_id, e.telegram_msg_id)

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
        subscribed = [ch for cid, ch in all_known.items() if cid in self.monitor.subscribed_ids]
        self.channel_panel.set_subscribed(subscribed)

        self.live_view.set_channel_titles({cid: ch.title for cid, ch in all_known.items()})
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
