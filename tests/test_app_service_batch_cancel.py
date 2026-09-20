"""2026-09-17 PR 4 mega-split:`AppService.cancel_current_batch` + 跨 batch 取消测试。

7 tests:
- cancel_current_batch 设 _cancel_event / 多次 set 安全
- cancel 在 pin / react 循环中段(与 mark_read 同模式)— 锁定在 test_app_service_batch_mark_read.py
- BatchProgress 含 elapsed_seconds + rate_per_second(v1.4.0+)— pin / react 两个 op 验证
- _compute_rate 除零返回零(v1.4.0+ 兜底)
"""

from __future__ import annotations

import time

from tgmonitor.core.app_service import AppService
from tgmonitor.core.events import BatchDone, BatchProgress, EventBus


async def test_cancel_current_batch_sets_event(batch_app: AppService) -> None:
    """v1.7.3:`cancel_current_batch()` 必须 set `_cancel_event`。"""
    assert not batch_app._cancel_event.is_set()  # type: ignore[attr-defined]
    batch_app.cancel_current_batch()
    assert batch_app._cancel_event.is_set()  # type: ignore[attr-defined]
    # 多次调用安全(再次 set 已 set 的 Event)
    batch_app.cancel_current_batch()
    assert batch_app._cancel_event.is_set()  # type: ignore[attr-defined]


async def test_cancel_pin_stops_mid_loop(batch_app: AppService, collected: list) -> None:
    """v1.7.3:`pin_messages` 取消 — break 后 stop 调 client。"""
    items = [(1, 1), (2, 2), (3, 3)]
    call_count = 0

    async def _side_effect(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            batch_app.cancel_current_batch()

    batch_app.client.pin_messages.side_effect = _side_effect  # type: ignore[attr-defined]
    result = await batch_app.pin_messages(items)
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
            batch_app.cancel_current_batch()

    batch_app.client.add_reaction.side_effect = _side_effect  # type: ignore[attr-defined]
    result = await batch_app.add_reaction(items, "🔥")
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
    batch_app.cancel_current_batch()
    batch_app.cancel_current_batch()
    batch_app.cancel_current_batch()
    assert batch_app._cancel_event.is_set()  # type: ignore[attr-defined]


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
    await batch_app.pin_messages(items)
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
    await batch_app.add_reaction(items, "🔥")
    assert len(received) >= 1
    last = received[-1]
    assert last.op == "react"
    assert last.processed == 2
    assert last.total == 2
    assert last.elapsed_seconds >= 0.0


async def test_compute_rate_zero_elapsed_returns_zero_rate(batch_app: AppService) -> None:
    """v1.7.4:`_compute_rate` 在 elapsed < 1e-3 时返 0(除零保护)。"""
    # 不调 _start_batch_timer,_batch_started_at = 0(初始),time.monotonic() - 0 = 当前时间(大)
    # 但 processed/elapsed 还是 > 0。真正测除零路径需 mock monotonic。
    # 简化:设 _batch_started_at = time.monotonic() — elapsed ≈ 0 → rate = 0
    batch_app._batch_started_at = time.monotonic()  # type: ignore[attr-defined]
    elapsed, rate = batch_app._compute_rate(5)
    assert elapsed >= 0.0
    assert rate == 0.0 or rate > 0.0  # 取决于 timer 精度;_compute_rate 路径不抛即可
    # 更严格:首次调用 `_start_batch_timer()` 后立即 `_compute_rate` 应 rate=0
    batch_app._start_batch_timer()
    elapsed2, rate2 = batch_app._compute_rate(5)
    assert rate2 == 0.0  # elapsed < 1ms
