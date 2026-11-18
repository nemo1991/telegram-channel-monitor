"""v2.0.5 契约 — "频道页刷新已加入频道" → 大盘计数卡片同步刷新。

根因(2026-11-17):频道页刷新走 `app.channels.refresh_joined_channels()` 写库
(tg_subscribed),但成功后没有事件通知大盘;大盘"已加入频道"数取自
storage.count_tg_subscribed(),只由 MainWindow._refresh_state 驱动,而刷新路径
不经过它 → 库变了、卡片 stale。本文件守:
- G1 成功校正写库后发布 `JoinedChannelsRefreshed`,count = 频道数;
- G2 两条保护路径(未 ready / 空返回)不写库也不发事件(RK-09);
- G3 大盘订阅该事件后重读库计数 + 频道统计;
- G4 该事件不进"最近活动"列表(非 9 类运维事件)。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.fixtures._in_memory_repository import InMemoryRepository
from tgmonitor.core.channel_service import ChannelService
from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import (
    ErrorOccurred,
    EventBus,
    JoinedChannelsRefreshed,
)
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def storage() -> InMemoryRepository:
    return InMemoryRepository()


def _service(
    bus: EventBus,
    storage: StorageRepository,
    joined: list[ChannelDTO] | None = None,
    state: str = "ready",
) -> tuple[ChannelService, AsyncMock]:
    client = AsyncMock(spec=TelegramClient)
    client.state = state
    client.list_joined_channels.return_value = joined if joined is not None else []
    svc = ChannelService(bus=bus, client=client, storage=storage, monitor=None)
    return svc, client


# ============================================================
# G1:成功校正写库 → 发 JoinedChannelsRefreshed
# ============================================================


async def test_success_publishes_joined_refreshed_with_count(
    bus: EventBus, storage: InMemoryRepository
) -> None:
    """G1:非空 TG 列表写库成功后发事件,count=频道数。"""
    seen: list[JoinedChannelsRefreshed] = []

    async def _on(e: JoinedChannelsRefreshed) -> None:
        seen.append(e)

    bus.subscribe(JoinedChannelsRefreshed, _on)
    joined = [ChannelDTO(id=1, title="新闻"), ChannelDTO(id=2, title="科技")]
    svc, _ = _service(bus, storage, joined=joined)

    await svc.refresh_joined_channels()

    assert len(seen) == 1
    assert seen[0].count == 2
    assert await storage.count_tg_subscribed() == 2


async def test_success_with_cascade_still_publishes_once(
    bus: EventBus, storage: InMemoryRepository
) -> None:
    """G1:发生级联退订时仍只发一次 JoinedChannelsRefreshed(退订另发 Unmonitored)。"""
    await storage.upsert_channel(
        ChannelDTO(id=9, title="退订频道", is_monitored=True, is_tg_subscribed=True)
    )
    seen: list[JoinedChannelsRefreshed] = []

    async def _on(e: JoinedChannelsRefreshed) -> None:
        seen.append(e)

    bus.subscribe(JoinedChannelsRefreshed, _on)
    svc, _ = _service(bus, storage, joined=[ChannelDTO(id=1, title="仍在 TG")])

    await svc.refresh_joined_channels()

    assert len(seen) == 1
    assert seen[0].count == 1


# ============================================================
# G2:两条保护路径不写库、不发刷新事件(RK-09)
# ============================================================


async def test_not_ready_does_not_publish(bus: EventBus, storage: InMemoryRepository) -> None:
    """G2:未 ready → 不发刷新事件,只发 ErrorOccurred。"""
    seen: list[JoinedChannelsRefreshed] = []
    errors: list[ErrorOccurred] = []

    async def _on(e: JoinedChannelsRefreshed) -> None:
        seen.append(e)

    async def _err(e: ErrorOccurred) -> None:
        errors.append(e)

    bus.subscribe(JoinedChannelsRefreshed, _on)
    bus.subscribe(ErrorOccurred, _err)
    svc, client = _service(bus, storage, joined=[ChannelDTO(id=1, title="x")], state="uninit")

    result = await svc.refresh_joined_channels()

    assert result == []
    assert seen == []
    assert len(errors) == 1
    client.list_joined_channels.assert_not_awaited()


async def test_empty_joined_does_not_publish(bus: EventBus, storage: InMemoryRepository) -> None:
    """G2:空返回([])→ 不发刷新事件、不做级联清零(RK-09)。"""
    await storage.upsert_channel(ChannelDTO(id=1, title="旧频道", is_tg_subscribed=True))
    seen: list[JoinedChannelsRefreshed] = []

    async def _on(e: JoinedChannelsRefreshed) -> None:
        seen.append(e)

    bus.subscribe(JoinedChannelsRefreshed, _on)
    svc, _ = _service(bus, storage, joined=[])

    result = await svc.refresh_joined_channels()

    assert result == []
    assert seen == []
    got = await storage.get_channel(1)
    assert got is not None and got.is_tg_subscribed is True  # 未被误清零


# ============================================================
# G3/G4:大盘订阅行为(UI 侧,用 stub app/monitor,无真实窗口)
# ============================================================

from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget  # noqa: E402


def _dashboard_stub(qapp: QApplication, joined_count: int = 7):  # noqa: ANN202
    from tgmonitor.core.config import Settings

    app = MagicMock()
    app.bus = EventBus()
    app.client = MagicMock()
    app.client.state = "ready"
    app.client.me = None
    app.settings = Settings.for_test()
    app.storage.count_tg_subscribed = AsyncMock(return_value=joined_count)
    app.storage.aggregate_per_channel = AsyncMock(return_value={})
    monitor = MagicMock()
    monitor.monitored_ids = []
    dash = DashboardWidget(app, monitor, channels_provider=lambda: {})
    return dash, app


@pytest.mark.asyncio
async def test_dashboard_rereads_count_on_event(qapp: QApplication) -> None:
    """G3:大盘收到 JoinedChannelsRefreshed → 重读库计数 + 频道统计。"""
    from unittest.mock import patch

    dash, _ = _dashboard_stub(qapp, joined_count=3)
    with (
        patch.object(dash, "_refresh_joined_count") as reread_count,
        patch.object(dash, "_refresh_per_channel_stats") as reread_stats,
    ):
        await dash._on_joined_refreshed(JoinedChannelsRefreshed(count=3))
    reread_count.assert_called_once_with()
    reread_stats.assert_called_once_with()
    dash.deleteLater()


@pytest.mark.asyncio
async def test_joined_refreshed_not_in_activity_list(qapp: QApplication) -> None:
    """G4:刷新事件不是 9 类运维事件,不进最近活动时间线。"""
    dash, app = _dashboard_stub(qapp)
    await dash._on_bus_event(JoinedChannelsRefreshed(count=5))
    assert dash.activity_list.count() == 0
    dash.deleteLater()
