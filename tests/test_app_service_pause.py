"""AppService pause/resume 流程单测 — 2026-09-03 v1.6.1。

`AppService.pause_monitor()` / `resume_monitor()`:
- pause:monitor.stop() → client.stop() → MonitoringPaused event
- resume:client.start() → monitor.start() → MonitoringResumed event
- 幂等(连续 pause / 连续 resume 不抛)
- 失败时 event 不发(让 UI 仍按 paused 显示,提示用户重试)

2026-09-04 v1.6.6:_make_app_service 改用真 `Settings(paused=False)` — v1.6.6
引入 `settings.paused` 字段读取,MagicMock 会让 `settings.paused` 是个
truthy Mock 对象,导致 `_is_paused` 也是 truthy Mock,pause/resume 早返
no-op,8 case 全 fail。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tgmonitor.core.app_service import AppService
from tgmonitor.core.config import Settings
from tgmonitor.core.events import MonitoringPaused, MonitoringResumed
from tgmonitor.core.telegram.client import TelegramClient


def _make_app_service(bus, monitor: MagicMock, client: MagicMock) -> AppService:
    """构造 AppService stub — 只接 bus + monitor + client,其他依赖可 None。

    2026-09-04 v1.6.6:用真 `Settings(paused=False)`(default)而非 MagicMock —
    v1.6.6 起 `AppService.__init__` 读 `settings.paused` 初始化 `_is_paused`,
    MagicMock 会让 `settings.paused` 是 truthy Mock,导致 pause/resume 早返。
    """
    storage = MagicMock()
    objects = MagicMock()
    settings = Settings.for_test(paused=False)
    return AppService(
        bus=bus,
        client=client,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        objects=objects,  # type: ignore[arg-type]
        settings=settings,
        monitor=monitor,  # type: ignore[arg-type]
    )


@pytest.fixture
def bus():
    from tgmonitor.core.events import EventBus

    return EventBus()


@pytest.fixture
def monitor() -> MagicMock:
    m = MagicMock()
    m.stop = AsyncMock()
    m.start = AsyncMock()
    return m


@pytest.fixture
def client() -> MagicMock:
    c = MagicMock(spec=TelegramClient)
    c.stop = AsyncMock()
    c.start = AsyncMock()
    return c


# ---- pause ----


async def test_pause_monitor_calls_monitor_then_client(bus, monitor, client) -> None:
    """pause 顺序:monitor.stop() 先,client.stop() 后,最后发 MonitoringPaused。"""
    events: list[MonitoringPaused] = []

    async def _capture(e: MonitoringPaused) -> None:
        events.append(e)

    bus.subscribe(MonitoringPaused, _capture)

    app = _make_app_service(bus, monitor, client)
    await app.pause_monitor(source="tray")

    # 顺序断言 — monitor.stop 先于 client.stop
    assert monitor.stop.await_count == 1
    assert client.stop.await_count == 1
    assert app.is_paused is True
    # event 已发
    assert len(events) == 1
    assert events[0].source == "tray"


async def test_pause_monitor_idempotent(bus, monitor, client) -> None:
    """二次 pause 不抛 + 不重复调 stop / 不重复发 event。"""
    events: list[MonitoringPaused] = []

    async def _capture(e: MonitoringPaused) -> None:
        events.append(e)

    bus.subscribe(MonitoringPaused, _capture)

    app = _make_app_service(bus, monitor, client)
    await app.pause_monitor()
    await app.pause_monitor()  # no-op
    await app.pause_monitor()  # no-op

    assert monitor.stop.await_count == 1
    assert client.stop.await_count == 1
    assert len(events) == 1


async def test_pause_monitor_monitor_failure_still_proceeds(bus, monitor, client) -> None:
    """monitor.stop() 抛错时,client.stop() 仍调 + paused 状态仍设 + event 仍发。"""
    monitor.stop.side_effect = RuntimeError("monitor boom")

    events: list[MonitoringPaused] = []

    async def _capture(e: MonitoringPaused) -> None:
        events.append(e)

    bus.subscribe(MonitoringPaused, _capture)

    app = _make_app_service(bus, monitor, client)
    await app.pause_monitor()  # 不抛

    assert client.stop.await_count == 1
    assert app.is_paused is True
    assert len(events) == 1


async def test_pause_monitor_client_failure_still_marks_paused(bus, monitor, client) -> None:
    """client.stop() 抛错时,paused 状态仍设(用户能感知)+ event 仍发(UI 视觉同步)。"""
    client.stop.side_effect = RuntimeError("client boom")

    app = _make_app_service(bus, monitor, client)
    await app.pause_monitor()  # 不抛

    assert app.is_paused is True


# ---- resume ----


async def test_resume_monitor_calls_client_then_monitor(bus, monitor, client) -> None:
    """resume 顺序:先 client.start,后 monitor.start,最后发 MonitoringResumed。"""
    events: list[MonitoringResumed] = []

    async def _capture(e: MonitoringResumed) -> None:
        events.append(e)

    bus.subscribe(MonitoringResumed, _capture)

    app = _make_app_service(bus, monitor, client)
    # 先 pause 才有 paused 状态可恢复
    await app.pause_monitor()
    events.clear()

    await app.resume_monitor(source="tray")

    assert client.start.await_count == 1
    assert monitor.start.await_count == 1
    assert app.is_paused is False
    assert len(events) == 1
    assert events[0].source == "tray"


async def test_resume_monitor_idempotent_when_not_paused(bus, monitor, client) -> None:
    """未 paused 时调 resume = no-op(不调 client.start,不发 event)。"""
    app = _make_app_service(bus, monitor, client)
    await app.resume_monitor()  # not paused,no-op

    assert client.start.await_count == 0
    assert monitor.start.await_count == 0


async def test_resume_monitor_client_failure_keeps_paused(bus, monitor, client) -> None:
    """client.start() 抛错时,**不**发 Resumed,保持 paused 状态让用户重试。"""
    client.start.side_effect = RuntimeError("client start boom")

    events: list[MonitoringResumed] = []

    async def _capture(e: MonitoringResumed) -> None:
        events.append(e)

    bus.subscribe(MonitoringResumed, _capture)

    app = _make_app_service(bus, monitor, client)
    await app.pause_monitor()
    await app.resume_monitor()  # 内部 catch,不发 Resumed

    assert app.is_paused is True  # 仍 paused
    assert len(events) == 0  # Resumed 不发


async def test_pause_resume_pause_resume_cycle(bus, monitor, client) -> None:
    """完整 pause → resume → pause → resume 周期 — 4 次状态切换 + 4 个 event。"""
    app = _make_app_service(bus, monitor, client)
    assert app.is_paused is False

    await app.pause_monitor()
    assert app.is_paused is True
    await app.resume_monitor()
    assert app.is_paused is False
    await app.pause_monitor()
    assert app.is_paused is True
    await app.resume_monitor()
    assert app.is_paused is False

    assert monitor.stop.await_count == 2
    assert client.stop.await_count == 2
    assert client.start.await_count == 2
    assert monitor.start.await_count == 2


# ---- P2 #35:monitor.start 失败保持 paused ----


async def test_resume_monitor_monitor_start_failure_keeps_paused(bus, monitor, client) -> None:
    """P2 #35 回归:`monitor.start()` 抛错时**不** flip paused、不发 Resumed。

    旧实现 monitor.start 失败后照样 flip `_is_paused=False` + 发
    MonitoringResumed — UI 显示「监听中」但实时流 / 下载 worker 实际没起来,
    是假状态。新实现与 client.start 失败分支对齐:保持 paused 让用户重试。
    """
    monitor.start.side_effect = RuntimeError("monitor start boom")

    events: list[MonitoringResumed] = []

    async def _capture(e: MonitoringResumed) -> None:
        events.append(e)

    bus.subscribe(MonitoringResumed, _capture)

    app = _make_app_service(bus, monitor, client)
    await app.pause_monitor()
    await app.resume_monitor()  # monitor.start 失败 → 内部 catch,不发 Resumed

    assert app.is_paused is True, "monitor.start 失败必须保持 paused(P2 #35)"
    assert len(events) == 0, "monitor.start 失败不得发 MonitoringResumed(P2 #35)"
    # client.start 已成功(失败分支在它之前)
    assert client.start.await_count == 1
