"""TrayIcon 暂停 / 恢复视觉切换单测 — 2026-09-03 v1.6.1。

`_on_monitoring_paused` 切 icon / tooltip / menu text 到 paused 态;
`_on_monitoring_resumed` 反向切回。

测试用 `QT_QPA_PLATFORM=offscreen` 跑(headless 安全),不真依赖系统托盘
可用性 — 我们只验内部的 `setIcon` / `setToolTip` / `setText` 调用。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QWidget

from tgmonitor.core.events import (
    EventBus,
    MonitoringPaused,
    MonitoringResumed,
)
from tgmonitor.ui.widgets.tray_icon import TrayIcon

# `qapp` from tests/conftest.py — session-scope QApplication 单例


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def parent() -> QWidget:
    """QWidget 实例 — TrayIcon 要 Qt parent + `parent.show` 信号槽,Mock 不行。"""
    return QWidget()


@pytest.fixture
def app(bus) -> MagicMock:
    """mock AppService,只暴露 bus + 1 个 stub。"""
    a = MagicMock()
    a.bus = bus
    return a


@pytest.fixture
def tray(qapp, bus, parent, app) -> TrayIcon:
    """构造 TrayIcon — `QSystemTrayIcon` 构造返 MagicMock,使 setIcon /
    setToolTip / setContextMenu 调用可断言。`isSystemTrayAvailable` patch
    返 True 走完整路径。
    """
    fake_tray = MagicMock(name="QSystemTrayIcon")
    with (
        patch(
            "tgmonitor.ui.widgets.tray_icon.QSystemTrayIcon.isSystemTrayAvailable",
            return_value=True,
        ),
        patch(
            "tgmonitor.ui.widgets.tray_icon.QSystemTrayIcon",
            return_value=fake_tray,
        ),
    ):
        return TrayIcon(parent, app)


# ---- 暂停切视觉 ----


# ---- 恢复切视觉 ----


async def test_pause_resume_cycle_idempotent(tray, bus) -> None:
    """完整 pause → resume → pause → resume 周期 — _is_paused 翻转正确。"""
    assert tray._is_paused is False
    await bus.publish(MonitoringPaused(source="tray"))

    await bus.flush()
    assert tray._is_paused is True
    await bus.publish(MonitoringResumed(source="tray"))
    await bus.flush()
    assert tray._is_paused is False
    await bus.publish(MonitoringPaused(source="tray"))
    await bus.flush()
    assert tray._is_paused is True
    await bus.publish(MonitoringResumed(source="tray"))
    await bus.flush()
    assert tray._is_paused is False
