"""SubscriptionService 直接单测 — 2026-09-03 v1.5.4 PR #P2。

`core/subscription_service.py` v1.5.0 PR #A2 抽出后全仓 0 个直接测试。
本文件补全 12 case,重点兜底 `unsubscribe_channel` 回归 hot-spot
(2026-07-31 修 SUBSCRIBED_DRIFT_ANALYSIS #A:之前 storage 失败被
`log.exception` 吞后仍 emit `ChannelUnsubscribed`,UI 移走视觉元素,
但 storage 持久化记录仍 `is_subscribed=True`,下次启动 reload → 该频道
被「恢复订阅」)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from tgmonitor.core.dto import ChannelDTO, MessageDTO
from tgmonitor.core.events import (
    ChannelSubscribed,
    ChannelUnsubscribed,
    EventBus,
)
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.subscription_service import SubscriptionService
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream


def _make_service(
    bus: EventBus, client: TelegramClient, storage: StorageRepository
) -> SubscriptionService:
    return SubscriptionService(bus=bus, client=client, storage=storage)


@pytest.fixture
def fake_storage() -> AsyncMock:
    s = AsyncMock(spec=StorageRepository)
    s.list_subscribed_channels.return_value = []
    s.list_channels.return_value = []
    s.list_messages.return_value = []
    return s


@pytest.fixture
def fake_client() -> AsyncMock:
    return AsyncMock(spec=TelegramClient)


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


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
    """v1.5.0 PR #A2 顺序约束:upsert 元数据 → set subscribed → emit。
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
# 消息查询
# ============================================================


async def test_list_messages_default_uses_subscribed_truth(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """channel_ids=None + include_unsubscribed=False → 走已订真理。"""
    fake_storage.list_subscribed_channels.return_value = [
        ChannelDTO(id=1, title="a"),
        ChannelDTO(id=2, title="b"),
    ]

    svc = _make_service(bus, fake_client, fake_storage)
    await svc.list_messages()

    fake_storage.list_subscribed_channels.assert_awaited_once_with()
    fake_storage.list_channels.assert_not_awaited()
    fake_storage.list_messages.assert_awaited_once()
    args = fake_storage.list_messages.await_args.args
    assert args[0] == [1, 2]  # channel_ids


async def test_list_messages_include_unsubscribed_uses_all_channels(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """v1.5.3 PR #D2 新参数 — 走 storage.list_channels()。"""
    fake_storage.list_channels.return_value = [
        ChannelDTO(id=1, title="a"),
        ChannelDTO(id=2, title="b"),
        ChannelDTO(id=3, title="c"),  # 未订阅
    ]

    svc = _make_service(bus, fake_client, fake_storage)
    await svc.list_messages(include_unsubscribed=True)

    fake_storage.list_channels.assert_awaited_once_with()
    fake_storage.list_subscribed_channels.assert_not_awaited()
    args = fake_storage.list_messages.await_args.args
    assert args[0] == [1, 2, 3]


async def test_list_messages_explicit_channel_ids_overrides_scope(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """channel_ids 显式传 → 不调 list_subscribed / list_channels。"""
    svc = _make_service(bus, fake_client, fake_storage)
    await svc.list_messages(channel_ids=[5, 6], include_unsubscribed=True)

    fake_storage.list_subscribed_channels.assert_not_awaited()
    fake_storage.list_channels.assert_not_awaited()
    args = fake_storage.list_messages.await_args.args
    assert args[0] == [5, 6]


async def test_list_messages_empty_channel_ids_returns_empty(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """已订 0 频道 → 短路返 [],不调 storage.list_messages。"""
    fake_storage.list_subscribed_channels.return_value = []
    svc = _make_service(bus, fake_client, fake_storage)
    result = await svc.list_messages()
    assert result == []
    fake_storage.list_messages.assert_not_awaited()


async def test_list_messages_passes_through_date_and_search(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """date_from/to / search / limit 透传到 storage.list_messages。"""
    fake_storage.list_subscribed_channels.return_value = [ChannelDTO(id=1, title="a")]
    date_from = datetime(2026, 9, 1, tzinfo=UTC)
    date_to = datetime(2026, 9, 3, tzinfo=UTC)

    svc = _make_service(bus, fake_client, fake_storage)
    await svc.list_messages(
        date_from=date_from,
        date_to=date_to,
        search="猫",
        limit=50,
    )
    args = fake_storage.list_messages.await_args.args
    assert args[0] == [1]
    assert args[1] == date_from
    assert args[2] == date_to
    assert args[3] == 50
    # search 是 keyword-only
    assert fake_storage.list_messages.await_args.kwargs.get("search") == "猫"


async def test_list_messages_returns_storage_result(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """list_messages 直接返 storage.list_messages 的结果,不做二次过滤。"""
    expected = [MessageDTO(id=1, channel_id=1, telegram_msg_id=1, text="x")]
    fake_storage.list_subscribed_channels.return_value = [ChannelDTO(id=1, title="a")]
    fake_storage.list_messages.return_value = expected

    svc = _make_service(bus, fake_client, fake_storage)
    result = await svc.list_messages()
    assert result is expected  # 同一对象(无 copy)


# ============================================================
# 实时流
# ============================================================


def test_subscribe_updates_proxies_client(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """`subscribe_updates` 是 sync 方法,直接转给 client.subscribe_updates。"""
    sentinel = MagicMock(spec=UpdateStream)
    fake_client.subscribe_updates.return_value = sentinel

    svc = _make_service(bus, fake_client, fake_storage)
    result = svc.subscribe_updates()
    assert result is sentinel
    fake_client.subscribe_updates.assert_called_once_with()
