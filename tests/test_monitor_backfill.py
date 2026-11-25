"""2026-09-17 PR 4 mega-split:MonitorService 实时接收 + 周期补拉(backfill)测试。

14 tests:
- 实时接收 + dedup(2):`_seen_ids` LRU 命中,白名单过滤
- 周期补拉(5):fill_gap / loop / silent_when_closing / unanchored_capped /
  not_ready_skip
- iter_chat_history 预热(2):unavailable_chat / warms_up_chat_then_yields
- backfill unavailable 频道(1):warn_once_and_continues
- _seen_ids 容量(1):evicts_when_over_limit

helper classes:`_BackfillClient` / `_ClosingClient` / `_UnavailableClient` /
`_ChatHistoryClient`(原 tests/test_monitor_and_app.py 内联,本文件集中)。
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from tests.conftest import make_message
from tests.fixtures._async import wait_for
from tgmonitor.core.dto import MessageDTO
from tgmonitor.core.events import MessageReceived
from tgmonitor.core.telegram.fake_client import FakeTelegramClient

# ============================================================
# 实时接收 + dedup
# ============================================================


async def test_monitor_receives_and_dedupes(monitor, storage, client, bus):
    monitor.set_whitelist([100])
    await monitor.start()
    try:
        # 发 3 条同 id + 1 条不同 id + 1 条不在白名单
        for _ in range(3):
            await client.simulate_incoming(make_message(channel_id=100, msg_id=1, text="dup"))
        await client.simulate_incoming(make_message(channel_id=100, msg_id=2, text="new"))
        await client.simulate_incoming(make_message(channel_id=999, msg_id=1, text="ignored"))
        # 确定性等 monitor 处理完(不用裸 sleep 0.2)
        assert await wait_for(lambda: storage.count_messages(100), timeout=2.0) == 2
        # 不在白名单的频道不应落库
        assert await storage.count_messages(999) == 0
    finally:
        await monitor.stop()


async def test_message_received_event_published(monitor, client, bus):
    seen: list = []
    bus.subscribe(MessageReceived, lambda e: seen.append(e))
    monitor.set_whitelist([100])
    await monitor.start()
    try:
        await client.simulate_incoming(make_message(channel_id=100, msg_id=1, text="evt"))
        assert await wait_for(
            lambda: any(getattr(e, "message", None) and e.message.text == "evt" for e in seen),
            timeout=2.0,
        ), "MessageReceived 没在 2s 内 emit"
    finally:
        await monitor.stop()


# ============================================================
# 周期补拉(backfill)— 断线 / 重启期间 updateNewMessage 不重放的兜底
# ============================================================


class _BackfillClient(FakeTelegramClient):
    """`iter_chat_history` 按真实 TDLib 语义 yield:最新在前(向旧递减)。

    conftest 的 `FakeTelegramClient` 为 channel_sync 的 resume 语义测试按
    升序 yield,与真实 TDLib 相反;补拉逻辑依赖"最新在前",这里给忠实版本。
    """

    def __init__(self, history: dict[int, list[int]]) -> None:
        """`history`:channel_id → telegram_msg_id 列表(最新在前)。

        补拉只应在登录成功(ready)后执行,故默认置 ready。
        """
        super().__init__()
        self._state = "ready"
        self._history = history

    async def iter_chat_history(  # type: ignore[override]
        self,
        channel_id: int,
        *,
        before_msg_id: int = 0,
        limit: int = 100,
    ) -> AsyncIterator[MessageDTO]:
        for mid in self._history.get(channel_id, []):
            await asyncio.sleep(0)  # 让出 loop,模仿网络
            yield make_message(channel_id=channel_id, msg_id=mid, text=f"backfill-{mid}")


async def test_backfill_fills_gap_and_skips_known(bus, storage, objectstore, settings):
    """库里已有 id=100;历史最新在前 [104..100] → 只补 104-101,100 不重复 emit。"""
    from tgmonitor.core.monitor.service import MonitorService

    client = _BackfillClient({100: [104, 103, 102, 101, 100]})
    await storage.save_message(make_message(channel_id=100, msg_id=100, text="known"))
    mon = MonitorService(bus, client, storage, objectstore, settings)
    mon.set_whitelist([100])
    seen: list[MessageReceived] = []
    bus.subscribe(MessageReceived, lambda e: seen.append(e))
    await mon._backfill_all()  # 不 start 也直接可用(不依赖实时流)
    msgs = await storage.list_messages([100])
    assert {m.telegram_msg_id for m in msgs} == {100, 101, 102, 103, 104}
    # 只对"新"消息 emit,已落库的 100 不重复通知
    assert {e.message.telegram_msg_id for e in seen} == {101, 102, 103, 104}


async def test_backfill_loop_runs_periodically_and_stops(bus, storage, objectstore, settings):
    """周期补拉:interval 调小后,start 后消息自动入库;stop 干净退出不再补。"""
    from tgmonitor.core.monitor.service import MonitorService

    client = _BackfillClient({100: [5, 4, 3, 2, 1]})
    mon = MonitorService(bus, client, storage, objectstore, settings)
    mon.set_whitelist([100])
    mon._BACKFILL_INTERVAL = 0.02  # type: ignore[assignment]
    await mon.start()
    try:
        # 等首轮全补(wait_for vs 裸 sleep 0.12 — 实测通常 <30ms 命中)
        async def _count_eq_5() -> bool:
            return await storage.count_messages(100) == 5

        assert await wait_for(_count_eq_5, timeout=2.0), (
            f"首轮 backfill 没在 2s 内补完 5 条,got {await storage.count_messages(100)}"
        )
    finally:
        await mon.stop()
    count_after_stop = await storage.count_messages(100)
    assert count_after_stop == 5


async def test_backfill_silent_when_client_closing(bus, storage, objectstore, settings):
    """close() 中补拉 → 静默返回(不抛、不落库、不打 traceback)。"""
    from tgmonitor.core.monitor.service import MonitorService
    from tgmonitor.core.telegram.errors import ClientClosingError

    class _ClosingClient(_BackfillClient):
        async def iter_chat_history(  # type: ignore[override]
            self, channel_id: int, *, before_msg_id: int = 0, limit: int = 100
        ) -> AsyncIterator[MessageDTO]:
            raise ClientClosingError()
            yield  # noqa: B018  — 让函数成为 async generator(否则 async for 拿不到异常)

    mon = MonitorService(bus, _ClosingClient({100: [1]}), storage, objectstore, settings)
    mon.set_whitelist([100])
    await mon._backfill_all()
    assert await storage.count_messages(100) == 0


async def test_backfill_unanchored_capped_to_max_page(bus, storage, objectstore, settings, caplog):
    """无锚点频道:只预热 `_BACKFILL_MAX_PAGE` 条,不把整段历史拉完。

    回归:`max_id=0` 时 `<= 0` 永假,旧实现每轮把整段历史翻完,几万条会撞
    flood wait / 制造长空窗。
    """
    import logging

    from tgmonitor.core.dto import ChannelDTO
    from tgmonitor.core.monitor.service import MonitorService

    client = _BackfillClient({100: [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]})
    await storage.upsert_channel(ChannelDTO(id=100, title="测试频道"))
    mon = MonitorService(bus, client, storage, objectstore, settings)
    mon._BACKFILL_MAX_PAGE = 3  # type: ignore[assignment]
    mon.set_whitelist([100])
    with caplog.at_level(logging.WARNING, logger="tgmonitor.core.monitor.backfill"):
        await mon._backfill_all()
    msgs = await storage.list_messages([100])
    assert {m.telegram_msg_id for m in msgs} == {10, 9, 8}
    # issue #128:hit cap 日志以频道名为主、ID 为辅
    assert "测试频道 (id=100) hit cap 3" in caplog.text


async def test_backfill_log_falls_back_to_hash_id_without_channel_meta(
    bus, storage, objectstore, settings, caplog
):
    """元数据缺失(频道从未落库)→ 日志降级 `#id`,不输出 None / 空名。"""
    import logging

    from tgmonitor.core.monitor.service import MonitorService

    client = _BackfillClient({100: [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]})
    mon = MonitorService(bus, client, storage, objectstore, settings)
    mon._BACKFILL_MAX_PAGE = 3  # type: ignore[assignment]
    mon.set_whitelist([100])
    with caplog.at_level(logging.WARNING, logger="tgmonitor.core.monitor.backfill"):
        await mon._backfill_all()
    assert "#100 hit cap 3" in caplog.text
    assert "None" not in caplog.text


async def test_backfill_skipped_when_not_ready(bus, storage, objectstore, settings):
    """未登录(非 ready)时补拉直接跳过:不拉取频道信息、不落库、不打 traceback。

    回归:app 启动时 monitor 先于登录完成启动,旧实现每轮都调 getChatHistory
    → TDLib 抛 "Client not started" → 每 30s 刷一轮 ERROR。登录成功后
    state 变 ready,下一轮补拉自动恢复。
    """
    from tgmonitor.core.monitor.service import MonitorService

    client = _BackfillClient({100: [10, 9, 8]})
    client._state = "phone_required"  # 未登录
    mon = MonitorService(bus, client, storage, objectstore, settings)
    mon.set_whitelist([100])
    await mon._backfill_all()
    assert await storage.count_messages(100) == 0


async def test_backfill_unavailable_channel_warns_once_and_continues(
    bus, storage, objectstore, settings, caplog
):
    """频道不可访问(ChatUnavailableError)→ 只 warning 一次、跳过、不刷 traceback;
    同轮其它频道照常补拉;恢复可用后下一轮自动继续。
    """
    import logging

    from tgmonitor.core.monitor.service import MonitorService
    from tgmonitor.core.telegram.errors import ChatUnavailableError

    class _UnavailableClient(_BackfillClient):
        """history 里标记为 unavailable 的频道抛 ChatUnavailableError。"""

        def __init__(self, history: dict[int, list[int]]) -> None:
            super().__init__(history)
            self.unavailable: set[int] = set()

        async def iter_chat_history(  # type: ignore[override]
            self, channel_id: int, *, before_msg_id: int = 0, limit: int = 100
        ) -> AsyncIterator[MessageDTO]:
            if channel_id in self.unavailable:
                raise ChatUnavailableError(channel_id, reason="Chat not found")
            async for m in super().iter_chat_history(
                channel_id, before_msg_id=before_msg_id, limit=limit
            ):
                yield m

    from tgmonitor.core.dto import ChannelDTO

    client = _UnavailableClient({100: [5, 4], 101: [3, 2, 1]})
    client.unavailable = {100}
    await storage.upsert_channel(ChannelDTO(id=100, title="测试频道"))
    mon = MonitorService(bus, client, storage, objectstore, settings)
    mon.set_whitelist([100, 101])

    with caplog.at_level(logging.WARNING, logger="tgmonitor.core.monitor.backfill"):
        await mon._backfill_all()
    assert await storage.count_messages(100) == 0  # 不可用频道不落库
    assert "unavailable" in caplog.text  # warning 而非 traceback
    assert "Traceback" not in caplog.text
    # issue #128:日志以频道名为主、ID 为辅
    assert "测试频道 (id=100)" in caplog.text
    assert await storage.count_messages(101) == 3  # 同轮其它频道照常补

    # 再跑一轮:warning 只打一次,不重复刷
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tgmonitor.core.monitor.backfill"):
        await mon._backfill_all()
    assert "unavailable" not in caplog.text

    # 恢复可用后:下一轮照常补,并清掉 suppression 标记
    client.unavailable = set()
    await mon._backfill_all()
    assert await storage.count_messages(100) == 2


# ============================================================
# _seen_ids LRU 容量
# ============================================================


async def test_seen_ids_cache_evicts_when_over_limit(monitor, client, bus) -> None:
    """推 10001 条不同 id → _seen_ids 收敛到 10000(OrderedDict LRU)。"""
    monitor.set_whitelist([100])
    await monitor.start()
    try:
        for mid in range(1, 10002):
            await client.simulate_incoming(
                make_message(channel_id=100, msg_id=mid, text=f"m-{mid}")
            )
        # LRU cap=10000;wait_for 确认 _seen_ids 收敛
        assert await wait_for(lambda: len(monitor._seen_ids) == 10000, timeout=3.0), (
            f"_seen_ids 没在 3s 内收敛到 10000,实为 {len(monitor._seen_ids)}"
        )
        # 最旧的(1-1=0)被踢
        assert (100, 1) not in monitor._seen_ids
        # 最新(10001)还在
        assert (100, 10001) in monitor._seen_ids
    finally:
        await monitor.stop()


# ============================================================
# 2026-11-25 P2 #30:skip_next_tick 死逻辑修复
# ============================================================


async def test_reconnect_kick_skips_next_periodic_tick(bus, storage, objectstore, settings):
    """P2 #30 回归:reconnect kick 后,下一轮周期 tick 跳过一次。

    `_handle_connection_state`(reconnect → 立即 kick)设置
    `skip_next_tick=True`;旧实现该标志无任何消费者 — 30s tick 紧接着
    kick 又跑一轮,造成回拉重复。新实现 `_backfill_loop` 在 tick 顶部
    消费标志(skip 一次)。

    可观测行为:kick 与下一次 backfill_all 调用的间隔 ≥ 2 个 tick 周期
    (旧实现 ≈ 1 个周期)。
    """
    import time as _time

    from tgmonitor.core.events import ConnectionStateChanged
    from tgmonitor.core.monitor.service import MonitorService

    client = _BackfillClient({100: [1]})
    mon = MonitorService(bus, client, storage, objectstore, settings)
    mon.set_whitelist([100])
    mon._BACKFILL_INTERVAL = 0.01  # type: ignore[assignment]
    stamps: list[float] = []

    async def _spy_backfill() -> None:
        stamps.append(_time.monotonic())

    mon._backfill_all = _spy_backfill  # type: ignore[method-assign]

    # reconnect kick(loop 未运行,无竞态):立即补拉一轮 + 置位 skip
    await mon._handle_connection_state(ConnectionStateChanged(state="ready"))
    assert len(stamps) == 1, "kick 应立即补拉一轮"
    assert mon._skip_next_tick is True, "kick 应置位 skip_next_tick"

    # 启动周期循环:首个 tick 应消费 skip(不调用),次轮起恢复周期补拉
    await mon.start()
    try:
        assert await wait_for(lambda: len(stamps) >= 2, timeout=2.0), (
            f"skip 消费后周期补拉未恢复:stamps={len(stamps)}"
        )
        gap = stamps[1] - stamps[0]
        # 旧实现(gap ≈ 1 周期 = 10ms)在本断言失败;新实现中间隔了一个
        # 被跳过的 tick,gap ≥ 2 周期。
        assert gap >= 0.015, (
            f"kick 与下一次 tick 补拉间隔 {gap * 1000:.1f}ms ≈ 1 周期 — "
            "skip_next_tick 未被消费(P2 #30)"
        )
    finally:
        await mon.stop()
