"""PR #A4 — MainWindow 与 tray 集成测试(轻量版)。

只测 closeEvent 拦截 → minimize-to-tray + tray「退出」→ _quit_app +
`_truly_quit` flag 行为。

策略:绕开 `MainWindow.__init__` 的重依赖(SettingsPage → 真 `.env` /
storage),通过 `MainWindow` 子类手动注入 `_tray` / `_truly_quit` /
`_tray_first_close_hint_shown`,单独验 closeEvent 拦截逻辑。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMainWindow, QStatusBar

from tgmonitor.ui.main_window import MainWindow


class _MinimalMainWindow(MainWindow):
    """绕开 MainWindow.__init__ 的重 init — 直接 QMainWindow 构造 +
    手装 closeEvent 所需的最小状态。

    不调 `self._build_ui()` / `self._build_menu()` / `self._build_tray()`
    / `self._wire_events()` 等(会拉真 SettingsPage / ChannelWidget)。
    """

    def __init__(self) -> None:
        QMainWindow.__init__(self)
        self.app = MagicMock()
        self.app.bus = MagicMock()
        self.app.bus.publish_threadsafe = MagicMock()
        self.loop = MagicMock()
        self._tray = None  # 测试按需 patch
        self._truly_quit = False
        self._tray_first_close_hint_shown = False
        self._shutdown_cb = None
        # v2.0.11:本文件主要回归「关掉设置 → 隐藏到托盘」,旧用例显式关掉
        # 新开关;默认开启的退出分支由 test_close_on_exit_* 覆盖。
        self._close_on_exit = lambda: False
        self.setStatusBar(QStatusBar(self))
        # 2026-10-01 v1.11.x:状态栏改自定义 StatusBar,closeEvent 调
        # `status_bar.show_message(...)`;_MinimalMainWindow 给个 stub 即可
        # (本测试只验 hide/ignore/NotificationRequested 路径,不关心状态栏文案)。
        self.status_bar = MagicMock()
        # 2026-10-10 v1.12.x:`_quit_app` / `_show_and_raise` thin delegate
        # 走 `self._lifecycle_ctrl`,但本 fixture 不走真 controller — stub
        # 一个,直接调 MainWindow 原方法体逻辑(本测试只验 `_truly_quit` /
        # `qt_app.quit()` 等基础行为,不关心 controller 内部)。
        from types import SimpleNamespace

        self._lifecycle_ctrl = SimpleNamespace(
            quit_app=lambda: self._legacy_quit_app(),
            show_and_raise=lambda: self._legacy_show_and_raise(),
        )
        # 保留旧实现供 stub 调(LifecycleController 抽提前的原方法体,本测试
        # 验「走对了 controller 委派」而非「controller 内部逻辑」,所以原
        # 行为在本 fixture 内重放,等 LifecycleController 单测覆盖完整逻辑)。

    def _legacy_quit_app(self) -> None:
        self._truly_quit = True
        qt_app = QApplication.instance()
        assert qt_app is not None, "QApplication gone before quit"
        qt_app.quit()

    def _legacy_show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()


def _make_close_event() -> QCloseEvent:
    """构造一个 QCloseEvent(abstract 不可直接构造,走 QEvent 替身)。"""
    # QCloseEvent 实际可构造 — type ignore 走通
    return QCloseEvent()  # type: ignore[abstract]

    # showNormal 把窗口从最小化恢复;offscreen 下 isMinimized 状态机不稳,
    # 只验证不抛


def test_close_on_exit_enabled_quits_instead_of_hiding(qapp: QApplication) -> None:
    """v2.0.11:设置开启时点 X 走 qt_app.quit(),不再 hide 到托盘。"""
    win = _MinimalMainWindow()
    fake_tray = MagicMock()
    fake_tray.is_active = True
    win._tray = fake_tray
    win.show()
    win._close_on_exit = lambda: True

    event = _make_close_event()
    mock_quit = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "PySide6.QtWidgets.QApplication.instance",
            classmethod(lambda cls: MagicMock(quit=mock_quit)),
        )
        win.closeEvent(event)

    assert win._truly_quit is True
    assert mock_quit.called
    assert event.isAccepted() is True
    # 不应发「已在后台运行」通知,也不应改首次关闭提示标记
    assert not win.app.bus.publish_threadsafe.called
    assert win._tray_first_close_hint_shown is False


def test_close_on_exit_disabled_keeps_minimize_to_tray(qapp: QApplication) -> None:
    """设置关闭时仍保留原行为:hide + ignore + 后台提示。"""
    win = _MinimalMainWindow()
    fake_tray = MagicMock()
    fake_tray.is_active = True
    win._tray = fake_tray
    win.show()
    win._close_on_exit = lambda: False

    event = _make_close_event()
    win.closeEvent(event)

    assert win.isVisible() is False
    assert event.isAccepted() is False
    assert win.app.bus.publish_threadsafe.called
