"""TrayIcon 暂停 / 恢复视觉切换单测 — 2026-09-03 v1.6.1。

`_on_monitoring_paused` 切 icon / tooltip / menu text 到 paused 态;
`_on_monitoring_resumed` 反向切回。

测试用 `QT_QPA_PLATFORM=offscreen` 跑(headless 安全),不真依赖系统托盘
可用性 — 我们只验内部的 `setIcon` / `setToolTip` / `setText` 调用。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from tgmonitor.core.events import (
    EventBus,
    MonitoringPaused,
    MonitoringResumed,
)
from tgmonitor.ui.widgets.tray_icon import TrayIcon


@pytest.fixture
def qt_app() -> QApplication:
    """Ensure QApplication exists — offscreen mode,SVG render 也能跑。"""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


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
def tray(qt_app, bus, parent, app) -> TrayIcon:
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


async def test_paused_event_swaps_icon_to_paused_variant(tray, bus) -> None:
    """paused 事件 → tray.setIcon 被调 + 调的是 load_paused_app_icon()。"""
    with patch("tgmonitor.ui.widgets.tray_icon.load_paused_app_icon") as mock_paused_icon:
        mock_paused_icon.return_value = MagicMock(name="paused_icon")
        await bus.publish(MonitoringPaused(source="tray"))
        import asyncio

        await asyncio.sleep(0.05)
    # tray.setIcon 被调
    assert tray._tray.setIcon.call_count == 1
    # 传入的 icon 是 load_paused_app_icon() 的返回值
    assert tray._tray.setIcon.call_args.args[0] is mock_paused_icon.return_value


async def test_paused_event_changes_tooltip(tray, bus) -> None:
    """paused 事件 → tooltip 改「⏸ tgmonitor · 暂停监听中」。"""
    await bus.publish(MonitoringPaused(source="tray"))
    import asyncio

    await asyncio.sleep(0.05)
    assert "⏸" in tray._tray.setToolTip.call_args.args[0]
    assert "暂停" in tray._tray.setToolTip.call_args.args[0]


async def test_paused_event_changes_menu_text(tray, bus) -> None:
    """paused 事件 → 菜单 action 文字「暂停监听」→「继续监听」。"""
    assert tray._action_pause.text() == "暂停监听"
    await bus.publish(MonitoringPaused(source="tray"))
    import asyncio

    await asyncio.sleep(0.05)
    assert tray._action_pause.text() == "继续监听"


async def test_paused_event_sets_internal_paused_flag(tray, bus) -> None:
    """paused 事件 → _is_paused 内部状态 True(给 resume handler 留 fallback)。"""
    assert tray._is_paused is False
    await bus.publish(MonitoringPaused(source="tray"))
    import asyncio

    await asyncio.sleep(0.05)
    assert tray._is_paused is True


# ---- 恢复切视觉 ----


async def test_resumed_event_swaps_icon_back(tray, bus) -> None:
    """resumed 事件 → tray.setIcon 调回 load_app_icon()。"""
    # 先 paused
    await bus.publish(MonitoringPaused(source="tray"))
    import asyncio

    await asyncio.sleep(0.05)
    with patch("tgmonitor.ui.widgets.tray_icon.load_app_icon") as mock_app_icon:
        mock_app_icon.return_value = MagicMock(name="app_icon")
        await bus.publish(MonitoringResumed(source="tray"))
        await asyncio.sleep(0.05)
    # 最近一次 setIcon 传的是 load_app_icon() 的返回值
    last_call = tray._tray.setIcon.call_args
    assert last_call.args[0] is mock_app_icon.return_value


async def test_resumed_event_restores_tooltip(tray, bus) -> None:
    """resumed 事件 → tooltip 改回原始「tgmonitor · Telegram 频道监听」。"""
    await bus.publish(MonitoringPaused(source="tray"))
    import asyncio

    await asyncio.sleep(0.05)
    await bus.publish(MonitoringResumed(source="tray"))
    await asyncio.sleep(0.05)
    tooltip = tray._tray.setToolTip.call_args.args[0]
    assert "暂停" not in tooltip
    assert "Telegram 频道监听" in tooltip


async def test_resumed_event_restores_menu_text(tray, bus) -> None:
    """resumed 事件 → 菜单文字「继续监听」→「暂停监听」。"""
    await bus.publish(MonitoringPaused(source="tray"))
    import asyncio

    await asyncio.sleep(0.05)
    assert tray._action_pause.text() == "继续监听"
    await bus.publish(MonitoringResumed(source="tray"))
    await asyncio.sleep(0.05)
    assert tray._action_pause.text() == "暂停监听"


async def test_pause_resume_cycle_idempotent(tray, bus) -> None:
    """完整 pause → resume → pause → resume 周期 — _is_paused 翻转正确。"""
    assert tray._is_paused is False
    await bus.publish(MonitoringPaused(source="tray"))
    import asyncio

    await asyncio.sleep(0.05)
    assert tray._is_paused is True
    await bus.publish(MonitoringResumed(source="tray"))
    await asyncio.sleep(0.05)
    assert tray._is_paused is False
    await bus.publish(MonitoringPaused(source="tray"))
    await asyncio.sleep(0.05)
    assert tray._is_paused is True
    await bus.publish(MonitoringResumed(source="tray"))
    await asyncio.sleep(0.05)
    assert tray._is_paused is False


async def test_paused_event_no_tray_is_silent(qt_app, bus, parent, app) -> None:
    """无系统托盘(offscreen)时 paused 事件不抛 — 内部 _tray 是 None,no-op。"""
    with patch(
        "tgmonitor.ui.widgets.tray_icon.QSystemTrayIcon.isSystemTrayAvailable",
        return_value=False,
    ):
        tray = TrayIcon(parent, app)
    assert tray._tray is None
    # 收 paused 事件不应抛
    await bus.publish(MonitoringPaused(source="tray"))
    import asyncio

    await asyncio.sleep(0.05)
    # 内部 _is_paused 仍会被设(供其他 UI 兜底判断用),但 _tray 路径不走
    assert tray._is_paused is True
