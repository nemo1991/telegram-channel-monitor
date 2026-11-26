"""2026-09-17 PR 4 mega-split:`AppService.cancel_current_batch` + 跨 batch 取消测试。

7 tests:
- cancel_current_batch set 当前 op 的局部 Event(P2 #31)/ op 结束置 None
- cancel 在 pin / react 循环中段
- 并发两个批量,cancel 只作用于最近注册的 op(P2 #31 回归)
- BatchProgress 含 elapsed_seconds + rate_per_second(v1.4.0+)— pin / react 两个 op 验证
- _compute_rate 除零返回零(v1.4.0+ 兜底)
"""

from __future__ import annotations

import asyncio
import time

from tgmonitor.core.app_service import AppService
from tgmonitor.core.events import BatchDone, BatchProgress, EventBus


async def test_cancel_current_batch_sets_current_op_event(
    batch_app: AppService, collected: list
) -> None:
    """v1.7.3 + P2 #31:cancel_current_batch set 当前 op 的局部 Event。

    旧实现是单一共享 Event(无 op 也 set,靠下一 op 开头 clear);新实现
    每 op 创建局部 Event,op 结束(含异常)置回 None — 无 op 进行时
    cancel 是 no-op,不会把取消信号悬挂到下一个 op 上。
    """
    observed: dict = {}

    async def _side_effect(*_args, **_kwargs):
        ev = batch_app.batch._cancel_event  # type: ignore[attr-defined]
        observed["during_op"] = ev
        observed["set_before"] = ev.is_set()
        batch_app.batch.cancel_current_batch()
        observed["set_after"] = ev.is_set()

    batch_app.client.pin_messages.side_effect = _side_effect  # type: ignore[attr-defined]
    await batch_app.batch.pin_messages([(1, 1)])

    assert observed["during_op"] is not None
    assert observed["set_before"] is False
    assert observed["set_after"] is True
    # op 结束后引用清空 → cancel 变 no-op(不抛、不悬挂)
    assert batch_app.batch._cancel_event is None  # type: ignore[attr-defined]
    batch_app.batch.cancel_current_batch()
    assert batch_app.batch._cancel_event is None  # type: ignore[attr-defined]


async def test_cancel_pin_stops_mid_loop(batch_app: AppService, collected: list) -> None:
    """v1.7.3:`pin_messages` 取消 — break 后 stop 调 client。"""
    items = [(1, 1), (2, 2), (3, 3)]
    call_count = 0

    async def _side_effect(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            batch_app.batch.cancel_current_batch()

    batch_app.client.pin_messages.side_effect = _side_effect  # type: ignore[attr-defined]
    result = await batch_app.batch.pin_messages(items)
    assert result == 1
    assert call_count == 1
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert len(done) == 1
    assert done[0].op == "pin"
    assert done[0].succeeded == 1
    assert done[0].error == "cancelled"


async def test_cancel_react_stops_mid_loop(batch_app: AppService, collected: list) -> None:
    """v1.7.3:`add_reaction` 取消 — break 后 stop 调 client。"""
    items = [(1, 10), (1, 11), (1, 12), (1, 13)]
    call_count = 0

    async def _side_effect(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            batch_app.batch.cancel_current_batch()

    batch_app.client.add_reaction.side_effect = _side_effect  # type: ignore[attr-defined]
    result = await batch_app.batch.add_reaction(items, "🔥")
    assert result == 1  # 第 1 条 RPC 已发出
    assert call_count == 1
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert len(done) == 1
    assert done[0].op == "react"
    assert done[0].succeeded == 1
    assert done[0].failed == 3
    assert done[0].error == "cancelled"


async def test_multiple_cancel_calls_safe(batch_app: AppService) -> None:
    """v1.7.3:多次 cancel_current_batch 调用安全 — Event 多次 set。"""
    batch_app.batch.cancel_current_batch()
    batch_app.batch.cancel_current_batch()
    batch_app.batch.cancel_current_batch()
    # 无 op 进行时 _cancel_event 为 None,cancel 是 no-op(P2 #31)
    assert batch_app.batch._cancel_event is None  # type: ignore[attr-defined]


async def test_concurrent_ops_cancel_targets_latest_op_only(
    batch_app: AppService, collected: list
) -> None:
    """P2 #31 回归:并发两个批量,cancel 只作用于最近注册的 op。

    旧实现单一 Event + 各 op 开头 clear():op2 启动后 cancel,set 的是
    共享 Event — op1 的循环也看到 is_set 被连带取消。新实现每 op 局部
    Event:op2(op1 之后注册)被取消,op1 不受影响跑完全部条目。
    """

    # op1:pin 5 个频道(每频道 1 条 → 5 次 RPC),每次 RPC 慢 0.02s
    async def _slow_pin(_cid, _mids, **_kw):
        await asyncio.sleep(0.02)

    batch_app.client.pin_messages.side_effect = _slow_pin  # type: ignore[attr-defined]

    # op2:add_reaction 2 条(逐条 RPC,慢 0.02s)
    async def _slow_react(_cid, _mid, _emoji, **_kw):
        await asyncio.sleep(0.02)

    batch_app.client.add_reaction.side_effect = _slow_react  # type: ignore[attr-defined]

    op1 = asyncio.create_task(
        batch_app.batch.pin_messages([(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])
    )
    await asyncio.sleep(0.005)  # 让 op1 注册自己的 Event 并进入第一条 RPC
    op2 = asyncio.create_task(batch_app.batch.add_reaction([(7, 1), (7, 2)], "🔥"))
    await asyncio.sleep(0.005)  # 让 op2 注册(覆盖"当前 op"引用)
    batch_app.batch.cancel_current_batch()  # 作用于 op2

    r1 = await op1
    r2 = await op2

    assert r2 == 1, f"op2 第 2 条前应被取消(实际成功 {r2})"
    assert r1 == 5, f"op1 不应被 op2 的 cancel 波及,应跑完全部条目(实际 {r1})"
    done = [e for e in collected if isinstance(e, BatchDone)]
    d1 = next(d for d in done if d.op == "pin")
    d2 = next(d for d in done if d.op == "react")
    assert d1.error is None
    assert d2.error == "cancelled"


async def test_batch_progress_emits_elapsed_and_rate_for_pin(
    bus: EventBus, batch_app: AppService
) -> None:
    """v1.7.4:pin_messages 每次 BatchProgress publish 含 `elapsed_seconds` /
    `rate_per_second` — 后者 = processed / elapsed。
    """
    received: list[BatchProgress] = []

    async def _on(e: BatchProgress) -> None:
        received.append(e)

    bus.subscribe(BatchProgress, _on)

    items = [(1, 1), (1, 2), (1, 3)]
    await batch_app.batch.pin_messages(items)
    # 至少 1 个 progress emit(3 条成功 → 1 个 emit)
    assert len(received) >= 1
    last = received[-1]
    assert last.op == "pin"
    assert last.processed == 3
    assert last.total == 3
    # elapsed ≥ 0,rate = processed / elapsed(> 0)
    assert last.elapsed_seconds >= 0.0
    if last.elapsed_seconds > 1e-3:
        assert last.rate_per_second > 0.0
        # 3 items / N 秒(测试机 N 通常 < 1s,rate > 3)
        assert abs(last.rate_per_second - 3 / last.elapsed_seconds) < 0.1


async def test_batch_progress_emits_elapsed_and_rate_for_react(
    bus: EventBus, batch_app: AppService
) -> None:
    """v1.7.4:add_reaction 同上。"""
    received: list[BatchProgress] = []

    async def _on(e: BatchProgress) -> None:
        received.append(e)

    bus.subscribe(BatchProgress, _on)

    items = [(1, 1), (1, 2)]
    await batch_app.batch.add_reaction(items, "🔥")
    assert len(received) >= 1
    last = received[-1]
    assert last.op == "react"
    assert last.processed == 2
    assert last.total == 2
    assert last.elapsed_seconds >= 0.0


async def test_compute_rate_zero_elapsed_returns_zero_rate(batch_app: AppService) -> None:
    """v1.7.4:`_compute_rate` 在 elapsed < 1e-3 时返 0(除零保护)。

    2026-11-11 v2.0.0 Phase 3:ETA helper 是 BatchService 私有方法,
    计时起点由 BatchService 自持(`batch_app.batch._batch_started_at`)。
    """
    # 不调 _start_batch_timer,_batch_started_at = 0(初始),time.monotonic() - 0 = 当前时间(大)
    # 但 processed/elapsed 还是 > 0。真正测除零路径需 mock monotonic。
    # 简化:设 _batch_started_at = time.monotonic() — elapsed ≈ 0 → rate = 0
    batch_app.batch._batch_started_at = time.monotonic()
    elapsed, rate = batch_app.batch._compute_rate(5)
    assert elapsed >= 0.0
    assert rate == 0.0 or rate > 0.0  # 取决于 timer 精度;_compute_rate 路径不抛即可
    # 更严格:首次调用 `_start_batch_timer()` 后立即 `_compute_rate` 应 rate=0
    batch_app.batch._start_batch_timer()
    elapsed2, rate2 = batch_app.batch._compute_rate(5)
    assert rate2 == 0.0  # elapsed < 1ms
