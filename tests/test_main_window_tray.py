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

from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow, QStatusBar  # noqa: E402

from tgmonitor.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """构造一次 QApplication — 多次跑 UI 测试不重复创建。"""
    app = QApplication.instance() or QApplication([])
    return app  # type: ignore[return-value]


class _MinimalMainWindow(MainWindow):
    """绕开 MainWindow.__init__ 的重 init — 直接 QMainWindow 构造 +
    手装 closeEvent 所需的最小状态。

    不调 `self._build_ui()` / `self._build_menu()` / `self._build_tray()`
    / `self._wire_events()` 等(会拉真 SettingsPage / ChannelWidget)。
    """

    def __init__(self) -> None:  # noqa: D401 — 故意不走 super().__init__
        QMainWindow.__init__(self)
        self.app = MagicMock()
        self.app.bus = MagicMock()
        self.app.bus.publish_threadsafe = MagicMock()
        self.loop = MagicMock()
        self._tray = None  # 测试按需 patch
        self._truly_quit = False
        self._tray_first_close_hint_shown = False
        self._shutdown_cb = None
        self.setStatusBar(QStatusBar(self))


def _make_close_event() -> QCloseEvent:
    """构造一个 QCloseEvent(abstract 不可直接构造,走 QEvent 替身)。"""
    # QCloseEvent 实际可构造 — type ignore 走通
    return QCloseEvent()  # type: ignore[abstract]


def test_minimal_close_no_tray_proceeds(qapp: QApplication) -> None:
    """`_tray=None` → closeEvent 不进 minimize-to-tray,直接 accept(默认路径)。"""
    win = _MinimalMainWindow()
    event = _make_close_event()
    win.closeEvent(event)
    assert event.isAccepted() is True


def test_minimal_close_minimizes_when_tray_active(qapp: QApplication) -> None:
    """`_tray.is_active=True` + _truly_quit=False → hide + ignore + 弹通知。"""
    win = _MinimalMainWindow()
    fake_tray = MagicMock()
    fake_tray.is_active = True
    win._tray = fake_tray
    win.show()
    assert win.isVisible() is True

    event = _make_close_event()
    win.closeEvent(event)

    # minimize + ignore
    assert win.isVisible() is False
    assert event.isAccepted() is False
    # publish_threadsafe 触发 NotificationRequested
    assert win.app.bus.publish_threadsafe.called
    # 提示只首次弹 — 标记已设
    assert win._tray_first_close_hint_shown is True


def test_minimal_close_subsequent_silent(qapp: QApplication) -> None:
    """第二次关窗:仍 minimize + ignore,但 publish_threadsafe 不再调。"""
    win = _MinimalMainWindow()
    fake_tray = MagicMock()
    fake_tray.is_active = True
    win._tray = fake_tray
    win.show()
    e1 = _make_close_event()
    win.closeEvent(e1)
    assert win.app.bus.publish_threadsafe.called
    win.app.bus.publish_threadsafe.reset_mock()

    win.show()
    e2 = _make_close_event()
    win.closeEvent(e2)
    assert win.isVisible() is False
    assert e2.isAccepted() is False
    assert not win.app.bus.publish_threadsafe.called


def test_minimal_close_truly_quit_skips_minimize(
    qapp: QApplication,
) -> None:
    """`_truly_quit=True` → 不 minimize(让 closeEvent 走真 shutdown 路径)。"""
    win = _MinimalMainWindow()
    fake_tray = MagicMock()
    fake_tray.is_active = True
    win._tray = fake_tray
    win._truly_quit = True
    win.show()

    event = _make_close_event()
    win.closeEvent(event)

    # accept(不 ignore)— 父类 super().closeEvent 接受关窗
    assert event.isAccepted() is True
    # minimize 不触发
    assert not win.app.bus.publish_threadsafe.called


def test_minimal_close_inactive_tray_skips_minimize(
    qapp: QApplication,
) -> None:
    """`_tray.is_active=False`(offscreen / Linux 无 indicator)→ 不 minimize。"""
    win = _MinimalMainWindow()
    fake_tray = MagicMock()
    fake_tray.is_active = False
    win._tray = fake_tray
    win.show()

    event = _make_close_event()
    win.closeEvent(event)

    assert event.isAccepted() is True
    assert not win.app.bus.publish_threadsafe.called


def test_quit_app_sets_truly_quit(qapp: QApplication) -> None:
    """`_quit_app` 标 _truly_quit=True + 调 qt_app.quit()(patch 避免真关)。"""
    win = _MinimalMainWindow()
    assert win._truly_quit is False
    mock_quit = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "tgmonitor.ui.main_window.QApplication.instance",
            classmethod(lambda cls: MagicMock(quit=mock_quit)),
        )
        win._quit_app()
    assert win._truly_quit is True
    assert mock_quit.called


def test_show_and_raise_restores_window(qapp: QApplication) -> None:
    """`_show_and_raise` 调 showNormal / raise_ / activateWindow(均 no-op 不抛)。"""
    win = _MinimalMainWindow()
    # minimize 状态
    win.showMinimized()
    win._show_and_raise()
    # showNormal 把窗口从最小化恢复;offscreen 下 isMinimized 状态机不稳,
    # 只验证不抛
