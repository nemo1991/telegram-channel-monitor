"""MessageService 直接单测 — 2026-11-11 v2.0.0 Phase 3-C1。

从 `test_subscription_service.py` 拆出消息查询部分(频道订阅归
`test_channel_service.py`)。锁定 `list_messages` 的「已订真理」语义 +
favorite_only / tag_only / pinned_only 3 kwarg 透传(2026-09-14 v1.7.5 PR #8)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from tgmonitor.core.dto import ChannelDTO, MessageDTO
from tgmonitor.core.events import EventBus
from tgmonitor.core.message_service import MessageService
from tgmonitor.core.storage.repository import StorageRepository


@pytest.fixture
def fake_storage() -> AsyncMock:
    s = AsyncMock(spec=StorageRepository)
    s.list_subscribed_channels.return_value = []
    s.list_channels.return_value = []
    s.list_messages.return_value = []
    return s


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


def _make_service(bus: EventBus, storage: StorageRepository) -> MessageService:
    return MessageService(bus=bus, storage=storage)


# ============================================================
# 消息查询
# ============================================================


async def test_list_messages_default_uses_subscribed_truth(
    bus: EventBus, fake_storage: AsyncMock
) -> None:
    """channel_ids=None + include_unsubscribed=False → 走已订真理。"""
    fake_storage.list_subscribed_channels.return_value = [
        ChannelDTO(id=1, title="a"),
        ChannelDTO(id=2, title="b"),
    ]

    svc = _make_service(bus, fake_storage)
    await svc.list_messages()

    fake_storage.list_subscribed_channels.assert_awaited_once_with()
    fake_storage.list_channels.assert_not_awaited()
    fake_storage.list_messages.assert_awaited_once()
    args = fake_storage.list_messages.await_args.args
    assert args[0] == [1, 2]  # channel_ids


async def test_list_messages_include_unsubscribed_uses_all_channels(
    bus: EventBus, fake_storage: AsyncMock
) -> None:
    """v1.5.3 PR #D2 新参数 — 走 storage.list_channels()。"""
    fake_storage.list_channels.return_value = [
        ChannelDTO(id=1, title="a"),
        ChannelDTO(id=2, title="b"),
        ChannelDTO(id=3, title="c"),  # 未订阅
    ]

    svc = _make_service(bus, fake_storage)
    await svc.list_messages(include_unsubscribed=True)

    fake_storage.list_channels.assert_awaited_once_with()
    fake_storage.list_subscribed_channels.assert_not_awaited()
    args = fake_storage.list_messages.await_args.args
    assert args[0] == [1, 2, 3]


async def test_list_messages_explicit_channel_ids_overrides_scope(
    bus: EventBus, fake_storage: AsyncMock
) -> None:
    """channel_ids 显式传 → 不调 list_subscribed / list_channels。"""
    svc = _make_service(bus, fake_storage)
    await svc.list_messages(channel_ids=[5, 6], include_unsubscribed=True)

    fake_storage.list_subscribed_channels.assert_not_awaited()
    fake_storage.list_channels.assert_not_awaited()
    args = fake_storage.list_messages.await_args.args
    assert args[0] == [5, 6]


async def test_list_messages_empty_channel_ids_returns_empty(
    bus: EventBus, fake_storage: AsyncMock
) -> None:
    """已订 0 频道 → 短路返 [],不调 storage.list_messages。"""
    fake_storage.list_subscribed_channels.return_value = []
    svc = _make_service(bus, fake_storage)
    result = await svc.list_messages()
    assert result == []
    fake_storage.list_messages.assert_not_awaited()


async def test_list_messages_passes_through_date_and_search(
    bus: EventBus, fake_storage: AsyncMock
) -> None:
    """date_from/to / search / limit 透传到 storage.list_messages。"""
    fake_storage.list_subscribed_channels.return_value = [ChannelDTO(id=1, title="a")]
    date_from = datetime(2026, 9, 1, tzinfo=UTC)
    date_to = datetime(2026, 9, 3, tzinfo=UTC)

    svc = _make_service(bus, fake_storage)
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


async def test_list_messages_returns_storage_result(bus: EventBus, fake_storage: AsyncMock) -> None:
    """list_messages 直接返 storage.list_messages 的结果,不做二次过滤。"""
    expected = [MessageDTO(id=1, channel_id=1, telegram_msg_id=1, text="x")]
    fake_storage.list_subscribed_channels.return_value = [ChannelDTO(id=1, title="a")]
    fake_storage.list_messages.return_value = expected

    svc = _make_service(bus, fake_storage)
    result = await svc.list_messages()
    assert result is expected  # 同一对象(无 copy)


# ============================================================
# 2026-09-14 v1.7.5 PR #8:`list_messages` 接 favorite_only / tag_only / pinned_only
# 3 kwarg → 透传给 storage.list_messages。
# ============================================================


def _seed_subscribed(fake_storage: AsyncMock) -> None:
    """让 `list_subscribed_channels` 返 1 个 channel — 不然 service 直接返 []。"""
    fake_storage.list_subscribed_channels.return_value = [ChannelDTO(id=1, title="a")]


async def test_pr8_list_messages_passes_favorite_only_kwarg(
    bus: EventBus, fake_storage: AsyncMock
) -> None:
    """PR #8:MessageService.list_messages(favorite_only=True) 透传给 storage。"""
    _seed_subscribed(fake_storage)
    svc = _make_service(bus, fake_storage)
    await svc.list_messages(favorite_only=True)
    fake_storage.list_messages.assert_awaited_once()
    kwargs = fake_storage.list_messages.await_args.kwargs
    assert kwargs.get("favorite_only") is True


async def test_pr8_list_messages_passes_tag_only_kwarg(
    bus: EventBus, fake_storage: AsyncMock
) -> None:
    """PR #8:MessageService.list_messages(tag_only=True) 透传给 storage。"""
    _seed_subscribed(fake_storage)
    svc = _make_service(bus, fake_storage)
    await svc.list_messages(tag_only=True)
    fake_storage.list_messages.assert_awaited_once()
    kwargs = fake_storage.list_messages.await_args.kwargs
    assert kwargs.get("tag_only") is True


async def test_pr8_list_messages_passes_pinned_only_kwarg(
    bus: EventBus, fake_storage: AsyncMock
) -> None:
    """PR #8:MessageService.list_messages(pinned_only=True) 透传给 storage。"""
    _seed_subscribed(fake_storage)
    svc = _make_service(bus, fake_storage)
    await svc.list_messages(pinned_only=True)
    fake_storage.list_messages.assert_awaited_once()
    kwargs = fake_storage.list_messages.await_args.kwargs
    assert kwargs.get("pinned_only") is True


async def test_pr8_list_messages_passes_all_3_kwargs(
    bus: EventBus, fake_storage: AsyncMock
) -> None:
    """PR #8:3 kwarg 同传(AND 语义)— 全透传到 storage.list_messages。"""
    _seed_subscribed(fake_storage)
    svc = _make_service(bus, fake_storage)
    await svc.list_messages(
        favorite_only=True,
        tag_only=True,
        pinned_only=True,
    )
    fake_storage.list_messages.assert_awaited_once()
    kwargs = fake_storage.list_messages.await_args.kwargs
    assert kwargs.get("favorite_only") is True
    assert kwargs.get("tag_only") is True
    assert kwargs.get("pinned_only") is True


async def test_pr8_list_messages_default_kwargs_false(
    bus: EventBus, fake_storage: AsyncMock
) -> None:
    """PR #8 regression:不传 kwargs → 默认 False。"""
    _seed_subscribed(fake_storage)
    svc = _make_service(bus, fake_storage)
    await svc.list_messages()
    fake_storage.list_messages.assert_awaited_once()
    kwargs = fake_storage.list_messages.await_args.kwargs
    # 默认值应明确为 False(不窄化)
    assert kwargs.get("favorite_only") is False
    assert kwargs.get("tag_only") is False
    assert kwargs.get("pinned_only") is False
