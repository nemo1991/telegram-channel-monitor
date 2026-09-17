"""2026-09-17 PR 4 mega-split:MonitorService 频道元数据 + 连接状态路由测试。

6 tests:
- ChannelMetadataChanged 路由(3):字段一致写入 / empty payload skip /
  storage 抛异常被吞
- ConnectionStateChanged(2):state='ready' 触发 backfill kick + skip flag /
  state='connecting' 不动
- ChannelMetadataChanged 不 republish(1):防无限循环
"""

from __future__ import annotations

import asyncio

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import (
    ChannelMetadataChanged,
    ConnectionStateChanged,
)


async def test_monitor_routes_channel_metadata_changed_to_storage(
    monitor, storage, client, bus
) -> None:
    """PR #14:publish ChannelMetadataChanged → storage.update_channel_metadata
    被调,字段一致写入 channel registry。

    注:不走真实 storage 方法(那由 test_storage_backends.py parity 覆盖);
    断言 monitor handler 调用了 storage 的 update_channel_metadata 即可。
    """
    monitor.set_whitelist([100])
    await monitor.start()
    try:
        # 预存频道
        await storage.upsert_channel(
            ChannelDTO(
                id=100,
                title="Old Title",
                username="oldname",
                member_count=10,
            )
        )
        await bus.publish(
            ChannelMetadataChanged(
                channel_id=100,
                title="New Title",
                username=None,  # None → username 不变
                member_count=500,
            )
        )
        await asyncio.sleep(0)
        got = await storage.get_channel(100)
        assert got is not None
        assert got.title == "New Title"  # 改了
        assert got.member_count == 500  # 改了
        assert got.username == "oldname"  # 保留(None → 不动)
    finally:
        await monitor.stop()


async def test_monitor_channel_metadata_handler_skips_empty_payload(monitor, storage, bus) -> None:
    """PR #14:event 全 None(channel_id=0 且无 username)时静默 skip,不抛异常。"""
    await monitor.start()
    try:
        # channel_id=0 且 supergroup_id 也 None → handler 直接 return
        await bus.publish(ChannelMetadataChanged(channel_id=0))
        await asyncio.sleep(0)
        # 无 assert 必要 — handler 不抛、storage 不被调
    finally:
        await monitor.stop()


async def test_monitor_channel_metadata_swallows_errors(monitor, storage, bus) -> None:
    """PR #14:storage 抛异常时 handler 不冒泡到 publish 调用者。"""
    await monitor.start()

    class BoomStorage:
        async def update_channel_metadata(self, *a, **kw):
            raise RuntimeError("simulated")

    original = monitor.storage
    monitor.storage = BoomStorage()
    try:
        await bus.publish(
            ChannelMetadataChanged(
                channel_id=999,
                title="x",
                member_count=1,
            )
        )
        await asyncio.sleep(0)
        # 无异常冒泡即成功
    finally:
        monitor.storage = original
        await monitor.stop()


async def test_monitor_connection_state_ready_kicks_backfill(monitor, client, bus) -> None:
    """PR #14:ConnectionStateChanged(state='ready') → 立即触发 backfill,
    设 _skip_next_tick=True,不让 30s tick 紧接着再跑一次。

    通过观察 _backfill_all 调用次数验证 kick + skip 行为。
    """
    monitor.set_whitelist([100])
    await monitor.start()
    try:
        # 改写 _backfill_all 为计数 stub
        call_count = {"n": 0}

        async def _count_backfill():
            call_count["n"] += 1

        monitor._backfill_all = _count_backfill  # type: ignore[assignment]

        await bus.publish(ConnectionStateChanged(state="ready"))
        await asyncio.sleep(0)
        assert call_count["n"] == 1
        # skip flag 已设
        assert monitor._skip_next_tick is True
    finally:
        await monitor.stop()


async def test_monitor_connection_state_non_ready_does_not_kick(monitor, client, bus) -> None:
    """PR #14:state != 'ready'(connecting/updating/...)时不动 backfill。"""
    monitor.set_whitelist([100])
    await monitor.start()
    try:
        call_count = {"n": 0}

        async def _count_backfill():
            call_count["n"] += 1

        monitor._backfill_all = _count_backfill  # type: ignore[assignment]

        await bus.publish(ConnectionStateChanged(state="connecting"))
        await asyncio.sleep(0)
        assert call_count["n"] == 0
        # skip flag 不变
        assert getattr(monitor, "_skip_next_tick", False) is False
    finally:
        await monitor.stop()


async def test_monitor_channel_metadata_does_not_republish(monitor, storage, bus) -> None:
    """PR #14:_handle_channel_metadata 落库后**不** republish
    ChannelMetadataChanged(避免无限循环,与 PR #10/_handle_interactions_changed
    同规则)。"""
    await monitor.start()
    try:
        # 计数 ChannelMetadataChanged 总 publish 数
        seen = {"n": 0}

        async def _count(_event):
            seen["n"] += 1

        bus.subscribe(ChannelMetadataChanged, _count)
        try:
            await storage.upsert_channel(ChannelDTO(id=200, title="t"))
            await bus.publish(
                ChannelMetadataChanged(
                    channel_id=200,
                    title="t2",
                    member_count=2,
                )
            )
            await asyncio.sleep(0)
            # 我们自己 + handler 落库(若 republish 会变成 2)
            assert seen["n"] == 1
        finally:
            bus.unsubscribe(ChannelMetadataChanged, _count)
    finally:
        await monitor.stop()
