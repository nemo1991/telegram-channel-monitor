"""v2.0.3 订阅/监听双字段契约 — 守 D5 数据模型拆分的关键不变量。

2026-11-17 v2.0.3:把"监听(monitored)"与"TG 订阅(tg_subscribed)"从同一字段
拆成两个独立概念。本文件守护:
- FR-06 双字段不恒等:取消监听不断 TG 订阅;TG 退订级联停监听(单向依赖);
- FR-07 迁移不回填:新列默认 False,历史值等价保留;
- FR-09 刷新不冲掉 monitored:落库只写 tg_subscribed;
- FR-12 级联停监听 + 空返回保护:不在 TG 列表则清标志摘白名单,空列表整轮跳过;
- RK-09 极高危:空返回 [] 绝不触发"全库退订"级联。

用 InMemoryRepository(语义与 SQLite/PG 对齐)+ mock client,纯 core 测试,无 Qt。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.fixtures._in_memory_repository import InMemoryRepository
from tgmonitor.core.channel_service import ChannelService
from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import (
    ChannelUnmonitored,
    ErrorOccurred,
    EventBus,
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
    monitor: object | None = None,
) -> tuple[ChannelService, AsyncMock]:
    client = AsyncMock(spec=TelegramClient)
    client.state = state
    client.list_joined_channels.return_value = joined if joined is not None else []
    svc = ChannelService(bus=bus, client=client, storage=storage, monitor=monitor)  # type: ignore[arg-type]
    return svc, client


# ============================================================
# FR-06 / FR-07:双字段独立 + 默认值
# ============================================================


async def test_two_flags_are_independent(bus: EventBus, storage: InMemoryRepository) -> None:
    """FR-06:监听与 TG 订阅是两个独立布尔,默认各自 False。"""
    ch = ChannelDTO(id=1, title="新闻")
    assert ch.is_monitored is False
    assert ch.is_tg_subscribed is False
    await storage.upsert_channel(ch)

    # 只开监听,TG 订阅仍 False
    await storage.set_channel_monitored(1, True)
    got = await storage.get_channel(1)
    assert got is not None and got.is_monitored is True and got.is_tg_subscribed is False

    # 只开 TG 订阅,监听不受影响
    await storage.set_channel_tg_subscribed(1, True)
    got = await storage.get_channel(1)
    assert got is not None and got.is_monitored is True and got.is_tg_subscribed is True


async def test_count_tg_subscribed_independent_of_monitored(
    bus: EventBus, storage: InMemoryRepository
) -> None:
    """FR-06:大盘'已加入'=tg_subscribed 计数,与监听数不同。"""
    await storage.upsert_channel(ChannelDTO(id=1, title="a", is_tg_subscribed=True))
    await storage.upsert_channel(ChannelDTO(id=2, title="b", is_tg_subscribed=True))
    await storage.upsert_channel(ChannelDTO(id=3, title="c", is_monitored=True))  # 监听但 TG 未订

    assert await storage.count_tg_subscribed() == 2
    assert len(await storage.list_monitored_channels()) == 1


# ============================================================
# FR-09:刷新落库不冲掉 monitored
# ============================================================


async def test_refresh_writes_tg_subscribed_without_touching_monitored(
    bus: EventBus, storage: InMemoryRepository
) -> None:
    """FR-09:TG 列表里的频道写 tg_subscribed=True,已监听的 monitored 不变。"""
    await storage.upsert_channel(
        ChannelDTO(id=1, title="新闻", is_monitored=True, is_tg_subscribed=False)
    )
    svc, _ = _service(bus, storage, joined=[ChannelDTO(id=1, title="新闻(新版)")])

    await svc.refresh_joined_channels()

    got = await storage.get_channel(1)
    assert got is not None
    assert got.is_tg_subscribed is True, "刷新应写 tg_subscribed"
    assert got.is_monitored is True, "刷新绝不能冲掉 monitored(白名单漂移)"


# ============================================================
# FR-12:TG 退订级联停监听(单向依赖 monitored ⇒ tg_subscribed)
# ============================================================


async def test_tg_unsubscribe_cascades_to_stop_monitoring(
    bus: EventBus, storage: InMemoryRepository
) -> None:
    """FR-12:库中有行但不在 TG 列表 → 双标志清 False + 摘白名单 + 发事件 + 留行历史。"""
    await storage.upsert_channel(
        ChannelDTO(id=1, title="退订频道", is_monitored=True, is_tg_subscribed=True)
    )
    monitor = MagicMock()
    events: list[ChannelUnmonitored] = []

    async def _on(e: ChannelUnmonitored) -> None:
        events.append(e)

    bus.subscribe(ChannelUnmonitored, _on)
    # TG 现在只剩频道 2 → 频道 1 视为已退订
    svc, _ = _service(bus, storage, joined=[ChannelDTO(id=2, title="仍在 TG")], monitor=monitor)

    await svc.refresh_joined_channels()

    got = await storage.get_channel(1)
    assert got is not None, "级联必须保留行(含历史),不删除"
    assert got.is_tg_subscribed is False
    assert got.is_monitored is False, "TG 退订必须级联停监听"
    monitor.remove_from_whitelist.assert_called_once_with(1)
    assert len(events) == 1 and events[0].channel_id == 1


async def test_tg_unsubscribe_keeps_unmonitored_channel_row(
    bus: EventBus, storage: InMemoryRepository
) -> None:
    """FR-12:本就未监听的频道 TG 退订 → 只清 tg_subscribed,不发 Unmonitored 事件。"""
    await storage.upsert_channel(
        ChannelDTO(id=1, title="未监听", is_monitored=False, is_tg_subscribed=True)
    )
    events: list[ChannelUnmonitored] = []

    async def _on(e: ChannelUnmonitored) -> None:
        events.append(e)

    bus.subscribe(ChannelUnmonitored, _on)
    svc, _ = _service(bus, storage, joined=[ChannelDTO(id=2, title="其他")])

    await svc.refresh_joined_channels()

    got = await storage.get_channel(1)
    assert got is not None and got.is_tg_subscribed is False and got.is_monitored is False
    assert events == [], "本就没监听,不应发取消监听事件"


# ============================================================
# FR-12 / RK-09:空返回保护(极高危)
# ============================================================


async def test_empty_joined_list_never_clears_subscriptions(
    bus: EventBus, storage: InMemoryRepository
) -> None:
    """RK-09:list_joined_channels 返 [] → 整轮跳过,库与白名单不变。"""
    await storage.upsert_channel(
        ChannelDTO(id=1, title="已监听", is_monitored=True, is_tg_subscribed=True)
    )
    monitor = MagicMock()
    svc, _ = _service(bus, storage, joined=[], state="ready", monitor=monitor)

    await svc.refresh_joined_channels()

    got = await storage.get_channel(1)
    assert got is not None and got.is_tg_subscribed is True and got.is_monitored is True
    monitor.remove_from_whitelist.assert_not_called()


async def test_not_ready_skips_refresh_entirely(bus: EventBus, storage: InMemoryRepository) -> None:
    """RK-09:未登录(state≠ready)→ 整轮跳过,不发请求、不改库、发错误提示。"""
    await storage.upsert_channel(
        ChannelDTO(id=1, title="已监听", is_monitored=True, is_tg_subscribed=True)
    )
    errors: list[ErrorOccurred] = []

    async def _on_err(e: ErrorOccurred) -> None:
        errors.append(e)

    bus.subscribe(ErrorOccurred, _on_err)
    svc, client = _service(bus, storage, joined=[ChannelDTO(id=2, title="x")], state="uninit")

    result = await svc.refresh_joined_channels()

    assert result == []
    client.list_joined_channels.assert_not_awaited()
    got = await storage.get_channel(1)
    assert got is not None and got.is_tg_subscribed is True and got.is_monitored is True
    assert any("未登录" in e.message for e in errors)


async def test_new_tg_channel_is_archived_not_monitored(
    bus: EventBus, storage: InMemoryRepository
) -> None:
    """FR-06:TG 新出现但库里没行的频道 → 建档,默认不监听。"""
    svc, _ = _service(bus, storage, joined=[ChannelDTO(id=99, title="新频道", kind="channel")])

    await svc.refresh_joined_channels()

    got = await storage.get_channel(99)
    assert got is not None
    assert got.is_tg_subscribed is True
    assert got.is_monitored is False, "新频道不应自动监听"
