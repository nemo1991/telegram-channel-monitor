"""ChannelService 直接单测 — 2026-11-11 v2.0.0 Phase 3-C1。

从 `test_subscription_service.py` 拆出频道部分(消息查询归
`test_message_service.py`)。重点兜底 `unmonitor_channel` 回归 hot-spot
(2026-07-31 修 SUBSCRIBED_DRIFT_ANALYSIS #A:之前 storage 失败被
`log.exception` 吞后仍 emit `ChannelUnmonitored`,UI 移走视觉元素,
但 storage 持久化记录仍 `is_monitored=True`,下次启动 reload → 该频道
被「恢复订阅」)。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tgmonitor.core.channel_service import ChannelService
from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import (
    ChannelMonitored,
    ChannelUnmonitored,
    EventBus,
)
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient


@pytest.fixture
def fake_storage() -> AsyncMock:
    s = AsyncMock(spec=StorageRepository)
    s.list_monitored_channels.return_value = []
    s.list_channels.return_value = []
    return s


@pytest.fixture
def fake_client() -> AsyncMock:
    client = AsyncMock(spec=TelegramClient)
    # v2.0.3 D7:域服务入口有 ready 门控,正常路径测试默认已登录。
    client.state = "ready"
    return client


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


async def test_list_monitored_channels_proxies_storage(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """`list_monitored_channels()` 走 storage,不走 client。"""
    fake_storage.list_monitored_channels.return_value = [ChannelDTO(id=1, title="a")]
    svc = _make_service(bus, fake_client, fake_storage)
    result = await svc.list_monitored_channels()
    assert result == [ChannelDTO(id=1, title="a")]
    fake_storage.list_monitored_channels.assert_awaited_once_with()
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
    fake_storage.list_monitored_channels.assert_not_awaited()


# ============================================================
# 订阅 / 退订
# ============================================================


async def test_monitor_channel_uses_atomic_upsert_then_emits(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """2026-11-25 P2 #37:monitor_channel 走 storage 原子入口。

    旧实现 `upsert_channel` + `set_channel_monitored` 两步独立写库,第二步
    失败留半状态;现在单入口 `upsert_channel_and_set_monitored` 一步完成,
    落库成功才 emit ChannelMonitored。
    """
    ch = ChannelDTO(id=1, title="news")
    events: list[ChannelMonitored] = []

    async def _on(e: ChannelMonitored) -> None:
        events.append(e)

    bus.subscribe(ChannelMonitored, _on)

    svc = _make_service(bus, fake_client, fake_storage)
    await svc.monitor_channel(ch)

    # 原子入口收到完整 DTO
    assert fake_storage.upsert_channel_and_set_monitored.await_count == 1
    assert fake_storage.upsert_channel_and_set_monitored.await_args.args[0] == ch
    # 旧两步路径不再出现
    fake_storage.upsert_channel.assert_not_awaited()
    fake_storage.set_channel_monitored.assert_not_awaited()
    # emit 事件(忽略 occurred_at 时间戳 — `Event` 基类 default_factory 每次都新生成)
    assert len(events) == 1
    assert events[0].channel == ch


async def test_unmonitor_channel_propagates_storage_errors(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """2026-07-31 SUBSCRIBED_DRIFT_ANALYSIS #A 回归 hot-spot:
    storage 失败必须 propagate,绝不能 log.exception 吞。
    """
    fake_storage.set_channel_monitored.side_effect = RuntimeError("db down")

    events: list[ChannelUnmonitored] = []

    async def _on(e: ChannelUnmonitored) -> None:
        events.append(e)

    bus.subscribe(ChannelUnmonitored, _on)

    svc = _make_service(bus, fake_client, fake_storage)
    with pytest.raises(RuntimeError, match="db down"):
        await svc.unmonitor_channel(channel_id=1)

    # 关键断言:storage 抛错时,事件**不能**被发出(否则 UI 假成功,
    # storage 没变,下次启动 reload → 「恢复订阅」)
    assert events == []
    # 验证 set_channel_monitored 被调(确认走到了 storage 路径)
    fake_storage.set_channel_monitored.assert_awaited_once_with(1, False)


async def test_unmonitor_channel_success_emits_event(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """正常路径:set_monitored(id, False) → emit ChannelUnmonitored。"""
    events: list[ChannelUnmonitored] = []

    async def _on(e: ChannelUnmonitored) -> None:
        events.append(e)

    bus.subscribe(ChannelUnmonitored, _on)

    svc = _make_service(bus, fake_client, fake_storage)
    await svc.unmonitor_channel(channel_id=42)

    fake_storage.set_channel_monitored.assert_awaited_once_with(42, False)
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


def test_monitored_ids_delegates_to_monitor(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """P2-M3:monitored_ids 经域服务读白名单,UI 不再直取 core monitor。"""
    monitor = MagicMock()
    monitor.monitored_ids = frozenset({1, 2, 3})
    svc = _make_service(bus, fake_client, fake_storage, monitor=monitor)

    assert svc.monitored_ids() == frozenset({1, 2, 3})


def test_monitored_ids_returns_empty_when_monitor_absent(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """P2-M3:monitor=None 启动期退化 → 空白名单(不抛,与读语义对齐)。"""
    svc = _make_service(bus, fake_client, fake_storage, monitor=None)
    assert svc.monitored_ids() == frozenset()


# ============================================================
# 2026-11-25 P2 #37:refresh_joined_channels 原子写路径
# ============================================================


async def test_refresh_joined_channels_uses_atomic_write_paths(
    bus: EventBus, fake_client: AsyncMock, fake_storage: AsyncMock
) -> None:
    """P2 #37 回归:refresh 级联的 per-channel 多步写收进 storage 原子入口。

    - TG 列表里的频道 → 单入口 `upsert_channel_metadata_and_set_tg_subscribed`;
    - 不在列表的 monitored 频道 → 单入口 `set_channel_flags(tg=False, monitored=False)`;
    - 不在列表的未监听频道 → `set_channel_flags(tg=False, monitored=None)`(不动标志)。
    旧实现每频道 2 条独立写库语句,中途异常留「元数据新 / 标志旧」半状态。
    """
    joined = ChannelDTO(id=10, title="joined")
    fake_client.list_joined_channels.return_value = [joined]
    fake_storage.list_channels.return_value = [
        ChannelDTO(id=10, title="joined"),
        ChannelDTO(id=20, title="left-monitored", is_monitored=True, is_tg_subscribed=True),
        ChannelDTO(id=30, title="left-idle", is_monitored=False),
    ]

    unmonitored: list[ChannelUnmonitored] = []

    async def _on(e: ChannelUnmonitored) -> None:
        unmonitored.append(e)

    bus.subscribe(ChannelUnmonitored, _on)

    svc = _make_service(bus, fake_client, fake_storage)
    await svc.refresh_joined_channels()

    # 1) joined 频道:原子入口一次到位,不再走旧两步
    fake_storage.upsert_channel_metadata_and_set_tg_subscribed.assert_awaited_once_with(joined)
    fake_storage.upsert_channel_metadata.assert_not_awaited()
    fake_storage.set_channel_tg_subscribed.assert_not_awaited()

    # 2) 级联退订:双标志单入口(monitored=False 仅对 monitored 频道)
    calls = fake_storage.set_channel_flags.await_args_list
    assert len(calls) == 2
    by_id = {c.args[0]: c.kwargs for c in calls}
    assert by_id[20] == {"tg_subscribed": False, "monitored": False}
    assert by_id[30] == {"tg_subscribed": False, "monitored": None}
    fake_storage.set_channel_monitored.assert_not_awaited()

    # 3) 事件只对 monitored 频道发
    assert [e.channel_id for e in unmonitored] == [20]
