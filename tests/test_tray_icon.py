"""PR #A4 — TrayIcon + 菜单 + 通知行为测试。

不在 CI 跑 system tray 本体(offscreen QPA 下 `isSystemTrayAvailable()`
返回 False),只测试:
  - `is_active` 行为
  - 菜单动作 publish 正确 QuitRequested(pause=True / False)
  - `on_notification` 把 NotificationRequested 转发到 showMessage
  - tray 不存在时 no-op
"""

from __future__ import annotations

import os

# offscreen:跑测试不弹真窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402

from tgmonitor.core.events import NotificationRequested, QuitRequested  # noqa: E402
from tgmonitor.ui.widgets.tray_icon import TrayIcon  # noqa: E402


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    """构造一次 QApplication — 多次跑 UI 测试不重复创建。"""
    app = QApplication.instance() or QApplication([])
    return app  # type: ignore[return-value]


class _DummyParent(QObject):
    """TrayIcon.parent 必须是 QObject(MagicMock 不能作 Qt parent)— 用真 QObject 占位。

    真正验证的是 tray 自身行为,与 parent 业务无关。
    """

    def __init__(self) -> None:
        super().__init__()
        self.show_called = False

    def show(self) -> None:  # noqa: D401 — 替 MagicMock
        self.show_called = True


def _make_app_svc() -> MagicMock:
    """构造 mock AppService — bus.publish 用普通 MagicMock(测试只验
    payload,不真 await;AsyncMock 会留 coroutine warning)。"""
    app = MagicMock()
    app.bus.publish = MagicMock()
    return app


def test_tray_inactive_when_no_system_tray(qt_app: QApplication) -> None:
    """offscreen QPA 无 system tray → is_active=False(UI 走 fallback)。"""
    parent = _DummyParent()
    app = _make_app_svc()
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
        tray = TrayIcon(parent, app)
    assert tray.is_active is False
    # hide / show / on_notification 全 no-op 不抛
    tray.show()
    tray.hide()


def test_tray_active_when_system_tray_available(qt_app: QApplication) -> None:
    """有 system tray 时 is_active=True + menu 3 项。"""
    parent = _DummyParent()
    app = _make_app_svc()
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
        tray = TrayIcon(parent, app)
    assert tray.is_active is True
    assert tray._menu is not None
    actions = [a.text() for a in tray._menu.actions() if a.text()]
    assert "显示主窗口" in actions
    assert "暂停监听" in actions
    assert "退出" in actions


def test_tray_show_action_shows_parent(qt_app: QApplication) -> None:
    """菜单「显示主窗口」→ parent.show() — 不走 quit。"""
    parent = _DummyParent()
    app = _make_app_svc()
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
        tray = TrayIcon(parent, app)
    show_action = next(a for a in tray._menu.actions() if a.text() == "显示主窗口")
    show_action.trigger()
    assert parent.show_called is True
    app.bus.publish.assert_not_called()


def test_tray_publishes_quit_requested_pause_on_pause(
    qt_app: QApplication,
) -> None:
    """菜单「暂停监听」→ bus.publish(QuitRequested(pause=True))。"""
    parent = _DummyParent()
    app = _make_app_svc()
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
        tray = TrayIcon(parent, app)
    pause_action = next(a for a in tray._menu.actions() if a.text() == "暂停监听")
    pause_action.trigger()
    app.bus.publish.assert_called_once()
    call_args = app.bus.publish.call_args
    event = call_args.args[0] if call_args.args else call_args.kwargs.get("event")
    assert isinstance(event, QuitRequested)
    assert event.pause is True


def test_tray_publishes_quit_requested_no_pause_on_quit(
    qt_app: QApplication,
) -> None:
    """菜单「退出」→ bus.publish(QuitRequested(pause=False))。"""
    parent = _DummyParent()
    app = _make_app_svc()
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
        tray = TrayIcon(parent, app)
    quit_action = next(a for a in tray._menu.actions() if a.text() == "退出")
    quit_action.trigger()
    app.bus.publish.assert_called_once()
    event = app.bus.publish.call_args.args[0]
    assert isinstance(event, QuitRequested)
    assert event.pause is False


def test_tray_double_click_shows_parent(qt_app: QApplication) -> None:
    """双击托盘 → parent.show()(系统级唤起主窗口)。"""
    parent = _DummyParent()
    app = _make_app_svc()
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
        tray = TrayIcon(parent, app)
    tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)
    assert parent.show_called is True


@pytest.mark.asyncio
async def test_tray_on_notification_forwards_to_show_message(
    qt_app: QApplication,
) -> None:
    """`on_notification` 把 NotificationRequested 转发到 tray.showMessage。"""
    parent = _DummyParent()
    app = _make_app_svc()
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
        tray = TrayIcon(parent, app)
    # mock showMessage
    tray._tray.showMessage = MagicMock()  # type: ignore[union-attr]
    event = NotificationRequested(level="info", title="测试标题", body="测试内容")
    await tray.on_notification(event)
    tray._tray.showMessage.assert_called_once()  # type: ignore[union-attr]
    args = tray._tray.showMessage.call_args.args  # type: ignore[union-attr]
    assert args[0] == "测试标题"
    assert args[1] == "测试内容"


@pytest.mark.asyncio
async def test_tray_on_notification_error_uses_critical_icon(
    qt_app: QApplication,
) -> None:
    """level=error → MessageIcon.Critical(其他 → Information)。"""
    parent = _DummyParent()
    app = _make_app_svc()
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
        tray = TrayIcon(parent, app)
    tray._tray.showMessage = MagicMock()  # type: ignore[union-attr]
    event = NotificationRequested(level="error", title="失败", body="爆了")
    await tray.on_notification(event)
    args = tray._tray.showMessage.call_args.args  # type: ignore[union-attr]
    # tray_icon.py 调 `showMessage(title, body, icon, 5000)` — icon 是第 3 位置
    assert args[2] == QSystemTrayIcon.MessageIcon.Critical


@pytest.mark.asyncio
async def test_tray_on_notification_noop_when_inactive(
    qt_app: QApplication,
) -> None:
    """is_active=False(无 system tray)时 on_notification no-op,不抛。"""
    parent = _DummyParent()
    app = _make_app_svc()
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
        tray = TrayIcon(parent, app)
    event = NotificationRequested(level="info", title="x", body="y")
    # 不抛即通过
    await tray.on_notification(event)
