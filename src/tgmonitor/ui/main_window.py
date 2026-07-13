"""主窗口 — 频道列表 | 消息流 | 工具栏。"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.dto import MessageDTO
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel
from tgmonitor.ui.widgets.export_dialog import ExportDialog
from tgmonitor.ui.widgets.login_dialog import LoginDialog
from tgmonitor.ui.widgets.message_view import MessageView
from tgmonitor.ui.widgets.settings_dialog import SettingsDialog

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(
        self,
        app: AppService,
        monitor: MonitorService,
        loop: asyncio.AbstractEventLoop,
        env_path: "Path | None" = None,
    ) -> None:
        super().__init__()
        self.app = app
        self.monitor = monitor
        self.loop = loop
        self.env_path = env_path or Path(".env")
        self.setWindowTitle("Telegram 频道监听 — tgmonitor")
        self.resize(1100, 700)

        self._vm = MonitorViewModel(app, monitor, loop)
        self._build_ui()
        self._wire_events()
        self._refresh_state()

    # ---- UI 装配 ----

    def _build_ui(self) -> None:
        # 工具栏
        tb = QToolBar("主工具栏")
        self.addToolBar(tb)
        self.act_login = QAction("登录", self)
        self.act_refresh = QAction("刷新频道", self)
        self.act_export = QAction("导出…", self)
        self.act_settings = QAction("设置…", self)
        tb.addAction(self.act_login)
        tb.addAction(self.act_refresh)
        tb.addSeparator()
        tb.addAction(self.act_export)
        tb.addSeparator()
        tb.addAction(self.act_settings)

        # 主分割:左频道 / 右消息
        splitter = QSplitter(Qt.Horizontal)

        # 左:频道面板
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(8, 8, 8, 8)
        lv.addWidget(QLabel("已监听频道"))
        self.channel_list = QListWidget()
        lv.addWidget(self.channel_list, 1)
        splitter.addWidget(left)

        # 右:消息流
        self.message_view = MessageView()
        splitter.addWidget(self.message_view)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        self.setCentralWidget(splitter)

        # 状态栏
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("未登录")

        # 信号
        self.act_login.triggered.connect(self._on_login)
        self.act_refresh.triggered.connect(self._on_refresh_channels)
        self.act_export.triggered.connect(self._on_export)
        self.act_settings.triggered.connect(self._on_settings)
        self.channel_list.itemDoubleClicked.connect(self._on_channel_toggle)

    # ---- 事件订阅 ----

    def _wire_events(self) -> None:
        # ViewModel 已经把 EventBus → Qt signal 转好,这里只接 Qt signal
        self._vm.message_received.connect(self._on_message_received)
        self._vm.login_state.connect(self._on_login_state)
        self._vm.channels_changed.connect(self._refresh_state)
        self._vm.export_done.connect(self._on_export_done)
        self._vm.error.connect(self._on_error)
        self._vm.settings_changed.connect(self._on_settings_changed)

    # ---- 槽 ----

    def _on_login(self) -> None:
        dlg = LoginDialog(self.app, self.loop, self)
        dlg.exec()
        self._refresh_state()

    def _on_refresh_channels(self) -> None:
        self._refresh_state()
        self._vm.refresh_joined_channels()

    def _on_channel_toggle(self, item: QListWidgetItem) -> None:
        cid = item.data(Qt.UserRole)
        if cid is None:
            return
        if cid in self.monitor._whitelist:  # type: ignore[attr-defined]
            self._vm.unsubscribe_channel(int(cid))
        else:
            ch = next(
                (c for c in self._vm.known_channels.values() if c.id == cid), None
            )
            if ch:
                self._vm.subscribe_channel(ch)

    def _on_export(self) -> None:
        if not self.monitor._whitelist:  # type: ignore[attr-defined]
            QMessageBox.information(self, "导出", "请先订阅至少一个频道")
            return
        dlg = ExportDialog(self.app, list(self.monitor._whitelist), self)  # type: ignore[attr-defined]
        if dlg.exec():
            self._vm.start_export(dlg.request())

    def _on_settings(self) -> None:
        dlg = SettingsDialog(self.app, self.loop, self.env_path, self)
        dlg.exec()

    def _on_settings_changed(self, what: str, needs_relogin: bool, backend_label: str) -> None:
        msg = f"已热重载: {what} → {backend_label}"
        self.statusBar().showMessage(msg, 5000)
        if needs_relogin:
            QMessageBox.information(
                self,
                "凭据已变更",
                "Telegram 凭据已变更。\n请重新登录以继续监听。",
            )

    # ---- 回调 ----

    def _on_message_received(self, dto_dict: dict) -> None:
        m = MessageDTO(**dto_dict)
        self.message_view.append(m)

    def _on_login_state(self, state: str) -> None:
        self.statusBar().showMessage(f"登录状态: {state}")

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

    def _refresh_state(self) -> None:
        self.channel_list.clear()
        all_known = self._vm.known_channels
        for cid, ch in sorted(all_known.items(), key=lambda kv: kv[1].title):
            item = QListWidgetItem(f"{'✓ ' if cid in self.monitor._whitelist else '   '}{ch.display}")
            item.setData(Qt.UserRole, cid)
            self.channel_list.addItem(item)
        # 拉一次历史
        self._vm.load_recent_messages()
