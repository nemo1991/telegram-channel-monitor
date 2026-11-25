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

from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402

from tgmonitor.core.events import QuitRequested  # noqa: E402
from tgmonitor.ui.widgets.tray_icon import TrayIcon  # noqa: E402

# `qapp` from tests/conftest.py — session-scope QApplication 单例


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
    """构造 mock AppService — bus.publish_async 用普通 MagicMock(测试只验
    payload,不真 await;AsyncMock 会留 coroutine warning)。

    2026-09-23 issue #21:TrayIcon 现在走 `publish_async`(fire-and-forget
    task)而不是 `publish`(会丢 coroutine 的 sync 调用)。
    """
    app = MagicMock()
    app.bus.publish_async = MagicMock()
    app.bus.publish = MagicMock()
    return app


def test_tray_publishes_quit_requested_pause_on_pause(
    qapp: QApplication,
) -> None:
    """菜单「暂停监听」→ bus.publish_async(QuitRequested(pause=True))。"""
    parent = _DummyParent()
    app = _make_app_svc()
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
        tray = TrayIcon(parent, app)
    pause_action = next(a for a in tray._menu.actions() if a.text() == "暂停监听")
    pause_action.trigger()
    app.bus.publish_async.assert_called_once()
    call_args = app.bus.publish_async.call_args
    event = call_args.args[0] if call_args.args else call_args.kwargs.get("event")
    assert isinstance(event, QuitRequested)
    assert event.pause is True
    # regression 2026-09-23 issue #21:不能调 publish(会丢 coroutine)
    app.bus.publish.assert_not_called()
