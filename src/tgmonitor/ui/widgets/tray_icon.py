# mypy: disable-error-code="attr-defined"
"""TrayIcon — 系统托盘图标 + 右键菜单 + 通知包装 — 2026-08-30 v1.5.0 PR #A4。

设计:
- 单例挂在 MainWindow 上;`QSystemTrayIcon.isSystemTrayAvailable()` 假
  (offscreen / Linux 无 systray)→ 整体退化为 None,UI 走 status bar fallback
- 右键菜单三动作:显示主窗口 / 暂停监听 / 退出 — 直接 publish
  QuitRequested 到 EventBus,无 main_window 直引用(防 GC 周期问题)
- `show_notification` 转发到 `QSystemTrayIcon.showMessage`;Linux
  notifications 不支持 `click_action`,UI 自己做主窗口「顶置」

使用:
    tray = TrayIcon(self, self.app)
    if tray.is_active:    # 有系统托盘时显示
        tray.show()
    self.app.bus.subscribe(NotificationRequested, tray.on_notification)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from tgmonitor.core.events import (
    NotificationRequested,
    QuitRequested,
)
from tgmonitor.ui.icon import load_app_icon

if TYPE_CHECKING:
    from PySide6.QtWidgets import QMainWindow

    from tgmonitor.core.app_service import AppService

log = logging.getLogger(__name__)


class TrayIcon(QObject):
    """系统托盘图标 — 包装 QSystemTrayIcon + 菜单 + 通知。

    不可用时(QApplication 平台不支持 / 无 indicator)→ `is_active=False`,
    调用方退化为仅 status bar。
    """

    def __init__(self, parent: QMainWindow, app: AppService) -> None:
        """构造 QSystemTrayIcon + 菜单;绑定 bus 事件订阅。"""
        super().__init__(parent)
        self._app = app
        self._parent = parent
        # isSystemTrayAvailable 检测 — 不可用时 _tray 留 None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.info("system tray not available on this platform")
            self._tray: QSystemTrayIcon | None = None
            self._menu: QMenu | None = None
            return
        self._tray = QSystemTrayIcon(load_app_icon(), parent)
        self._tray.setToolTip("tgmonitor · Telegram 频道监听")
        # 右键菜单
        self._menu = QMenu()
        self._action_show = QAction("显示主窗口", self._menu)
        self._action_show.triggered.connect(parent.show)
        self._menu.addAction(self._action_show)
        self._menu.addSeparator()
        # 暂停监听 — 2026-08-30 v1.5.0 PR #A4:仅 emit 事件,MonitorService
        # 暂停逻辑留 v1.5.1(目前只是事件出口,无订阅者)。
        self._action_pause = QAction("暂停监听", self._menu)
        self._action_pause.triggered.connect(lambda: app.bus.publish(QuitRequested(pause=True)))
        self._menu.addAction(self._action_pause)
        self._action_quit = QAction("退出", self._menu)
        self._action_quit.triggered.connect(lambda: app.bus.publish(QuitRequested(pause=False)))
        self._menu.addAction(self._action_quit)
        self._tray.setContextMenu(self._menu)
        # 左键双击 = 显示主窗口(单触发在 Linux 不稳)
        self._tray.activated.connect(self._on_activated)

    @property
    def is_active(self) -> bool:
        """是否真有系统托盘 — False 时 UI 走 fallback(status bar)。"""
        return self._tray is not None

    def show(self) -> None:
        """显示托盘图标(无系统托盘时 no-op)。"""
        if self._tray is not None:
            self._tray.show()

    def hide(self) -> None:
        """隐藏托盘图标(无系统托盘时 no-op)。"""
        if self._tray is not None:
            self._tray.hide()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """托盘激活回调 — DoubleClick → 显示主窗口。

        Linux 一些 DE 上 DoubleClick 不报,改 Trigger 同义 — 但 Trigger 会被
        context menu 触发时同时触发,容易误开;保守只 DoubleClick。
        """
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._parent.show()

    async def on_notification(self, event: object) -> None:
        """NotificationRequested 事件订阅 — 转发到 QSystemTrayIcon.showMessage。

        无系统托盘时 no-op(UI 端另订阅走 status bar)。
        """
        if not isinstance(event, NotificationRequested):
            return
        if self._tray is None:
            return
        # level → icon 映射
        icon = (
            QSystemTrayIcon.MessageIcon.Information
            if event.level != "error"
            else QSystemTrayIcon.MessageIcon.Critical
        )
        self._tray.showMessage(event.title, event.body, icon, 5000)
