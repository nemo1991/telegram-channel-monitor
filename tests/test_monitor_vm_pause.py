"""MonitorViewModel QuitRequested(pause=True) 切暂停/恢复 — 2026-09-03 v1.6.1。

tray 发 QuitRequested(pause=True) → VM 查 app.is_paused → 调
app.pause_monitor() / resume_monitor()。同事件源 toggle,期望幂等。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tgmonitor.core.events import EventBus, QuitRequested
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def app_mock(bus) -> MagicMock:
    a = MagicMock()
    a.bus = bus  # VM 内部 _wire_bus 走 self.app.bus
    a.is_paused = False
    a.pause_monitor = AsyncMock()
    a.resume_monitor = AsyncMock()
    return a


@pytest.fixture
def monitor_mock() -> MagicMock:
    return MagicMock()


@pytest.fixture
def vm(bus, app_mock, monitor_mock) -> MonitorViewModel:
    """构造 VM(不走标准 __init__,手填 3 个核心字段)。"""
    import asyncio

    loop = asyncio.new_event_loop()
    return MonitorViewModel(app=app_mock, monitor=monitor_mock, loop=loop)


# ---- toggle 逻辑 ----


async def test_quit_pause_when_not_paused_calls_pause(vm, app_mock, bus) -> None:
    """未 paused 时收到 QuitRequested(pause=True) → 调 pause_monitor。"""
    app_mock.is_paused = False
    await bus.publish(QuitRequested(pause=True))
    # 等 in-flight publish task
    import asyncio

    await asyncio.sleep(0.05)

    app_mock.pause_monitor.assert_awaited_once()
    app_mock.resume_monitor.assert_not_awaited()


async def test_quit_pause_when_paused_calls_resume(vm, app_mock, bus) -> None:
    """已 paused 时收到 QuitRequested(pause=True) → 调 resume_monitor。"""
    app_mock.is_paused = True
    await bus.publish(QuitRequested(pause=True))
    import asyncio

    await asyncio.sleep(0.05)

    app_mock.resume_monitor.assert_awaited_once()
    app_mock.pause_monitor.assert_not_awaited()


async def test_quit_no_pause_emits_qt_signal(vm, app_mock, bus) -> None:
    """QuitRequested(pause=False) → emit Qt quit_requested signal,不动 pause。"""
    received: list[None] = []
    vm.quit_requested.connect(lambda: received.append(None))
    await bus.publish(QuitRequested(pause=False))
    import asyncio

    await asyncio.sleep(0.05)

    assert len(received) == 1
    app_mock.pause_monitor.assert_not_awaited()
    app_mock.resume_monitor.assert_not_awaited()


async def test_quit_pause_passes_source_tray(vm, app_mock, bus) -> None:
    """pause / resume 调用都带 source='tray'(从 tray 触发)。"""
    app_mock.is_paused = False
    await bus.publish(QuitRequested(pause=True))
    import asyncio

    await asyncio.sleep(0.05)
    app_mock.pause_monitor.assert_awaited_once_with(source="tray")

    # 切到 paused 状态再发
    app_mock.is_paused = True
    await bus.publish(QuitRequested(pause=True))
    await asyncio.sleep(0.05)
    app_mock.resume_monitor.assert_awaited_once_with(source="tray")


async def test_quit_pause_non_quit_event_ignored(vm, app_mock, bus) -> None:
    """非 QuitRequested 事件不触发 pause/resume(总线 fan-out 兜底)。"""
    from tgmonitor.core.events import Event

    await bus.publish(Event())
    import asyncio

    await asyncio.sleep(0.05)
    app_mock.pause_monitor.assert_not_awaited()
    app_mock.resume_monitor.assert_not_awaited()
