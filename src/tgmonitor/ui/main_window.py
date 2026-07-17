"""主窗口 — 左侧栏(账户 + 频道) + 右侧消息流。

侧栏布局:
  - AccountWidget:凭据表单 + 登录状态 + 登录动作
  - ChannelWidget:已加入(双击订阅) + 已监听(双击退订)

工具栏:`刷新` `导出` `设置` — **不再有「登录」,登录入口已上移到侧栏**。

退出路径:
  - 用户点关窗 → `closeEvent` → 同步阻塞跑 async shutdown(用
    `run_coroutine_threadsafe` + `concurrent.futures.Future.result(timeout=...)`)
    → accept event,Qt 进入 quit。
  - 关键:aiotdlib `client.close()`(拆 TDLib 内部 thread + queue)**必须**在
    CFRunLoop 仍合法持有 mutex 的阶段完成,否则 macOS 上会抛
    `std::system_error: mutex lock failed: Invalid argument`(libcpp 析构
    路径上抢已 finalize 的 mutex)。`aboutToQuit` 时 loop 已进入 quit 过渡
    状态,不再安全 → 关窗路径走 closeEvent 同步阻塞、aboutToQuit 走尽力清理。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.dto import MessageDTO
from tgmonitor.ui.icon import action_icon
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel
from tgmonitor.ui.widgets.account_widget import AccountWidget
from tgmonitor.ui.widgets.channel_widget import ChannelWidget
from tgmonitor.ui.widgets.export_dialog import ExportDialog
from tgmonitor.ui.widgets.message_view import MessageView
from tgmonitor.ui.widgets.settings_dialog import SettingsDialog

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)


ShutdownCb = Callable[[], Awaitable[None]]


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
        # 进程级 `QGuiApplication.setWindowIcon(load_app_icon())` 已经在 app.py:120
        # 设过;再单独给主窗口设一次是浪费 lru_cache slot,且会 shadow OS-level
        # dock 图标可被覆盖的场景(例如 PyInstaller 包的 .icns)。
        self.resize(1180, 740)

        self._vm = MonitorViewModel(app, monitor, loop)
        self._shutdown_cb: ShutdownCb | None = None
        self._build_ui()
        self._wire_events()
        self._refresh_state()
        # 启动后主动拉一次 joined 列表 — 不然 known_channels 是空,
        # 即便 storage 里已订阅过频道,UI 下栏算 `subscribed` 交集也是空。
        # 见 MonitorViewModel.bootstrap_ui 的注释。
        self._vm.bootstrap_ui()

    def set_shutdown_callback(self, cb: ShutdownCb) -> None:
        """app.py 在 run() 里挂上 shutdown 协程,closeEvent 里同步阻塞跑它。

        为什么不在 aboutToQuit 里跑:
          - aboutToQuit 时 Qt 已进入 quit 过渡状态,qasync 事件循环上
            跑 aiotdlib client.close() 会撞 macOS CFRunLoop mutex 析构,
            抛 `std::system_error: mutex lock failed: Invalid argument`。
          - closeEvent 在 Qt 退出流程更早的阶段,loop 还活着,安全。
        """
        self._shutdown_cb = cb

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt 事件覆盖,按 Qt 命名约定
        """用户点关窗 → 同步阻塞跑 async shutdown(<= 10s)→ 接受 quit。

        关键:`asyncio.run_coroutine_threadsafe(...).result(timeout=...)` 等
        future 完成时,需要目标 asyncio loop 实际在跑(协程 tick 才会推进)。
        Qt 在 closeEvent 处理过程中**不会**自己 pump 事件,所以我们每 50ms
        调一次 `QApplication.processEvents()` 让 loop 跑一会儿,协程才有机会
        推进。否则永远 10s 超时。

        异常路径:
          - 协程被 cancel(loop shutdown / 用户二次 quit)→ `fut.cancelled()`;
            `CancelledError` 是 `BaseException` 不是 `Exception`,**必须**单独接,
            否则会从 closeEvent 抛出 → Qt 弹 "Error calling Python override"。
          - 协程自身抛 → log 后放行,不阻塞 quit。
          - 超时 10s → log warning 放行。
        """
        if self._shutdown_cb is not None:
            try:
                import concurrent.futures

                from PySide6.QtWidgets import QApplication

                fut = asyncio.run_coroutine_threadsafe(
                    self._shutdown_cb(), self.loop
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
                # 收尾:取结果。cancelled 是合法状态(coros 已被 loop 取消,
                # 比如用户连按 cmd+Q),不当作异常。
                if fut.cancelled():
                    log.warning("shutdown coroutine was cancelled")
                elif fut.done():
                    try:
                        fut.result(timeout=0)
                    except concurrent.futures.CancelledError:
                        # 竞争:cancelled 标志已 set 但 done() 路径还没看到
                        log.warning("shutdown coroutine was cancelled (race)")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("shutdown raised: %s: %s",
                                    type(exc).__name__, exc)
            except RuntimeError:
                # loop 已关(罕见):吃错误,让 quit 继续
                log.warning("loop unavailable during shutdown")
            except BaseException:  # noqa: BLE001
                # 最后一道闸:任何意外(包括 CancelledError)都不应让
                # Qt closeEvent 抛回主循环导致 "Error calling Python override"。
                log.exception("closeEvent: unexpected error in shutdown")
        super().closeEvent(event)

    # ---- UI 装配 ----

    def _build_ui(self) -> None:
        # 工具栏(3 动作:刷新 / 导出 / 设置)
        tb = QToolBar("主工具栏")
        tb.setMovable(False)
        self.addToolBar(tb)

        self.act_refresh = QAction(action_icon("refresh"), "刷新频道", self)
        self.act_refresh.setToolTip("从 Telegram 拉取当前账号加入的全部频道")
        tb.addAction(self.act_refresh)

        tb.addSeparator()

        self.act_export = QAction(action_icon("export"), "导出…", self)
        self.act_export.setToolTip("把已监听频道的消息导出为 JSON / CSV / Markdown / HTML")
        tb.addAction(self.act_export)

        tb.addSeparator()

        self.act_settings = QAction(action_icon("settings"), "设置…", self)
        self.act_settings.setToolTip("后端、对象存储、媒体策略、代理 等低频配置")
        tb.addAction(self.act_settings)

        # 中央:左侧栏 + 右消息流
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # --- 左:侧栏(账户 + 频道 双栏) ---
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(8)

        self.account_panel = AccountWidget(self.app, self.loop, self.env_path, left)
        self.channel_panel = ChannelWidget(self.app, self.loop, left)

        lv.addWidget(self.account_panel)
        lv.addWidget(self.channel_panel, 1)
        splitter.addWidget(left)

        # --- 右:消息流 ---
        self.message_view = MessageView()
        splitter.addWidget(self.message_view)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 820])
        self.setCentralWidget(splitter)

        # 状态栏
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("未登录")

        # 信号
        self.act_refresh.triggered.connect(self._on_refresh_channels)
        self.act_export.triggered.connect(self._on_export)
        self.act_settings.triggered.connect(self._on_settings)
        self.channel_panel.btn_refresh.clicked.connect(self._on_refresh_channels)
        self.channel_panel.sync_requested.connect(self._on_sync_requested)

    # ---- EventBus → UI(Qt signal 已在 ViewModel 里订阅好) ----

    def _wire_events(self) -> None:
        self._vm.message_received.connect(self._on_message_received)
        self._vm.login_state.connect(self._on_login_state)
        self._vm.channels_changed.connect(self._refresh_state)
        self._vm.export_done.connect(self._on_export_done)
        self._vm.error.connect(self._on_error)
        self._vm.settings_changed.connect(self._on_settings_changed)

    # ---- 工具栏槽 ----

    def _on_refresh_channels(self) -> None:
        self.statusBar().showMessage("拉取频道列表…", 2000)
        self._vm.refresh_joined_channels()

    def _on_export(self) -> None:
        if not self.monitor.subscribed_ids:
            QMessageBox.information(self, "导出", "请先订阅至少一个频道")
            return
        # 导出需要"已订阅的 id 列表"这语义没变;走 MonitorService 公开 API
        ids = sorted(int(cid) for cid in self.monitor.subscribed_ids)
        # export_dialog 只用 channel_ids 字段,DTO 是渲染细节;此处跳过
        dlg = ExportDialog(self.app, ids, self)
        if dlg.exec():
            self._vm.start_export(dlg.request())

    def _on_settings(self) -> None:
        dlg = SettingsDialog(self.app, self.loop, self.env_path, self)
        dlg.exec()

    # ---- 事件回调 ----

    def _on_message_received(self, m: MessageDTO) -> None:
        # 每次都同步标题表:VM.known_channels 在 channels_changed 时已更新,
        # 但实时消息可能来自一个刚被发现但还没刷新到 known_channels 的频道。
        # 把当前的 known_channels 透给 view,渲染时查表。
        # m 是 MessageDTO 本身(VM 直接 emit,不是 asdict)
        self.message_view.set_channel_titles(
            {cid: ch.title for cid, ch in self._vm.known_channels.items()}
        )
        self.message_view.append(m)

    def _on_login_state(self, state: str) -> None:
        self.statusBar().showMessage(f"登录状态: {state}", 4000)

    def _on_export_done(self, result: dict | None, error: str | None) -> None:
        if error:
            QMessageBox.critical(self, "导出失败", error)
        elif result:
            QMessageBox.information(
                self,
                "导出完成",
                f"已写入 {result['out_path']}\n{result['message_count']} 条消息,{result['bytes_written']} 字节",
            )

    def _on_error(self, msg: str) -> None:
        log.warning("error: %s", msg)
        self.statusBar().showMessage(f"⚠ {msg}", 5000)

    def _on_settings_changed(self, what: str, needs_relogin: bool, backend_label: str) -> None:
        msg = f"已热重载: {what} → {backend_label}"
        self.statusBar().showMessage(msg, 5000)
        if needs_relogin:
            QMessageBox.information(
                self,
                "凭据已变更",
                "Telegram 凭据已变更。\n请重新登录以继续监听。",
            )

    def _on_sync_requested(self, channel_ids: list[int]) -> None:
        """侧栏多选 + 全量同步 — 弹 options + 进度对话框,启动后台 task。"""
        from tgmonitor.core.dto import SyncOptions
        from tgmonitor.ui.widgets.sync_dialog import (
            SyncOptionsDialog,
            SyncProgressDialog,
        )

        # 取已选频道的 title 给对话框显示
        titles: dict[int, str] = {}
        for cid in channel_ids:
            ch = self._vm.known_channels.get(cid)
            if ch is not None:
                titles[cid] = ch.title
            else:
                titles[cid] = f"#{cid}"

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
        # 订阅 VM 的 sync signal → 进度对话框
        self._vm.sync_progress.connect(progress_dlg.on_progress)
        self._vm.sync_done.connect(progress_dlg.on_done)

        async def _go() -> None:
            try:
                await self.app.sync_channels(channel_ids, options)
            finally:
                # 断开信号(避免下一次 sync 又绑一次)
                try:
                    self._vm.sync_progress.disconnect(progress_dlg.on_progress)
                    self._vm.sync_done.disconnect(progress_dlg.on_done)
                except (RuntimeError, TypeError):
                    pass

        asyncio.run_coroutine_threadsafe(_go(), self.loop)
        progress_dlg.exec()

    def _refresh_state(self) -> None:
        """channels_changed 事件回调:把已知频道渲染到 channel_panel 的两栏。"""
        # 把 _vm.known_channels 灌到 channel_panel:
        # - 已加入(joined):vm.known_channels 的全集
        # - 已监听(subscribed):filter 出 _subscribed
        all_known = self._vm.known_channels
        self.channel_panel.set_joined(list(all_known.values()))
        subscribed = [
            ch for cid, ch in all_known.items()
            if cid in self.monitor.subscribed_ids
        ]
        self.channel_panel.set_subscribed(subscribed)
        # 同步标题表给 MessageView,后续历史消息(下面的 load_recent_messages)
        # 渲染时就能用真实频道名而不是 #id。
        self.message_view.set_channel_titles(
            {cid: ch.title for cid, ch in all_known.items()}
        )
        # 拉一次最近消息
        self._vm.load_recent_messages()
