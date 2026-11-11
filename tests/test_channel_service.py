"""ChannelService 直接单测 — 2026-11-11 v2.0.0 Phase 3-C1。

从 `test_subscription_service.py` 拆出频道部分(消息查询归
`test_message_service.py`)。重点兜底 `unsubscribe_channel` 回归 hot-spot
(2026-07-31 修 SUBSCRIBED_DRIFT_ANALYSIS #A:之前 storage 失败被
`log.exception` 吞后仍 emit `ChannelUnsubscribed`,UI 移走视觉元素,
但 storage 持久化记录仍 `is_subscribed=True`,下次启动 reload → 该频道
被「恢复订阅」)。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tgmonitor.core.channel_service import ChannelService
from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import (
    ChannelSubscribed,
    ChannelUnsubscribed,
    EventBus,
)
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient


@pytest.fixture
def fake_storage() -> AsyncMock:
    s = AsyncMock(spec=StorageRepository)
    s.list_subscribed_channels.return_value = []
    s.list_channels.return_value = []
    return s


@pytest.fixture
def fake_client() -> AsyncMock:
    return AsyncMock(spec=TelegramClient)


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


def _make_service(
    bus: EventBus,
    client: TelegramClient,
    storage: StorageRepository,
    monitor: object | None = None,
) -> ChannelService:
    return ChannelService(bus=bus, client=client, storage=storage, monitor=monitor)  # type: ignore[arg-type]


# ============================================================
# 频道列表
# ============================================================


async def test_list_subscribed_channels_proxies_storage(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """`list_subscribed_channels()` 走 storage,不走 client。"""
    fake_storage.list_subscribed_channels.return_value = [ChannelDTO(id=1, title="a")]
    svc = _make_service(bus, fake_client, fake_storage)
    result = await svc.list_subscribed_channels()
    assert result == [ChannelDTO(id=1, title="a")]
    fake_storage.list_subscribed_channels.assert_awaited_once_with()
    fake_client.list_joined_channels.assert_not_awaited()


async def test_list_joined_channels_proxies_client(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """`list_joined_channels()` 走 client,不走 storage(best-effort UX)。"""
    fake_client.list_joined_channels.return_value = [ChannelDTO(id=10, title="tg")]
    svc = _make_service(bus, fake_client, fake_storage)
    result = await svc.list_joined_channels()
    assert result == [ChannelDTO(id=10, title="tg")]
    fake_client.list_joined_channels.assert_awaited_once_with()
    fake_storage.list_subscribed_channels.assert_not_awaited()


# ============================================================
# 订阅 / 退订
# ============================================================


async def test_subscribe_channel_upserts_then_subscribes_then_emits(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """顺序约束:upsert 元数据 → set subscribed → emit。
    顺序反了会导致 metadata 丢失。
    """
    ch = ChannelDTO(id=1, title="news")
    events: list[ChannelSubscribed] = []

    async def _on(e: ChannelSubscribed) -> None:
        events.append(e)

    bus.subscribe(ChannelSubscribed, _on)

    svc = _make_service(bus, fake_client, fake_storage)
    await svc.subscribe_channel(ch)

    # upsert 收到完整 DTO
    assert fake_storage.upsert_channel.await_count == 1
    assert fake_storage.upsert_channel.await_args.args[0] == ch
    # set_subscribed 收到 (id, True)
    assert fake_storage.set_channel_subscribed.await_count == 1
    assert fake_storage.set_channel_subscribed.await_args.args == (1, True)
    # emit 事件(忽略 occurred_at 时间戳 — `Event` 基类 default_factory 每次都新生成)
    assert len(events) == 1
    assert events[0].channel == ch
    # 顺序:upsert 必先于 set_subscribed(用 mock call_args_list 抓顺序)
    call_order: list[str] = []
    for c in fake_storage.mock_calls:
        name = c[0]
        if name in ("upsert_channel", "set_channel_subscribed"):
            call_order.append(name)
    assert call_order == ["upsert_channel", "set_channel_subscribed"], (
        f"顺序错:call_order={call_order}"
    )


async def test_unsubscribe_channel_propagates_storage_errors(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """2026-07-31 SUBSCRIBED_DRIFT_ANALYSIS #A 回归 hot-spot:
    storage 失败必须 propagate,绝不能 log.exception 吞。
    """
    fake_storage.set_channel_subscribed.side_effect = RuntimeError("db down")

    events: list[ChannelUnsubscribed] = []

    async def _on(e: ChannelUnsubscribed) -> None:
        events.append(e)

    bus.subscribe(ChannelUnsubscribed, _on)

    svc = _make_service(bus, fake_client, fake_storage)
    with pytest.raises(RuntimeError, match="db down"):
        await svc.unsubscribe_channel(channel_id=1)

    # 关键断言:storage 抛错时,事件**不能**被发出(否则 UI 假成功,
    # storage 没变,下次启动 reload → 「恢复订阅」)
    assert events == []
    # 验证 set_channel_subscribed 被调(确认走到了 storage 路径)
    fake_storage.set_channel_subscribed.assert_awaited_once_with(1, False)


async def test_unsubscribe_channel_success_emits_event(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """正常路径:set_subscribed(id, False) → emit ChannelUnsubscribed。"""
    events: list[ChannelUnsubscribed] = []

    async def _on(e: ChannelUnsubscribed) -> None:
        events.append(e)

    bus.subscribe(ChannelUnsubscribed, _on)

    svc = _make_service(bus, fake_client, fake_storage)
    await svc.unsubscribe_channel(channel_id=42)

    fake_storage.set_channel_subscribed.assert_awaited_once_with(42, False)
    assert len(events) == 1
    assert events[0].channel_id == 42


# ============================================================
# 监听白名单(sync,转 MonitorService)
# ============================================================


def test_add_to_whitelist_delegates_to_monitor(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """白名单热路径 sync 转发 monitor.add_to_whitelist。"""
    monitor = MagicMock()
    svc = _make_service(bus, fake_client, fake_storage, monitor)
    svc.add_to_whitelist(42)
    monitor.add_to_whitelist.assert_called_once_with(42)


def test_remove_from_whitelist_delegates_to_monitor(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """退订路径 sync 转发 monitor.remove_from_whitelist。"""
    monitor = MagicMock()
    svc = _make_service(bus, fake_client, fake_storage, monitor)
    svc.remove_from_whitelist(42)
    monitor.remove_from_whitelist.assert_called_once_with(42)


def test_whitelist_without_monitor_raises(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """monitor=None(未启动)时白名单操作 assert 锁死契约,不静默 no-op。"""
    svc = _make_service(bus, fake_client, fake_storage, monitor=None)
    with pytest.raises(AssertionError):
        svc.add_to_whitelist(42)
    with pytest.raises(AssertionError):
        svc.remove_from_whitelist(42)
