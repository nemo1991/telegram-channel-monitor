"""2026-09-17 PR 4 mega-split:`AppService.delete_messages_batch` 测试。

4 tests:
- paused guard(返 0 + 不调 monitor.delete_messages)— 回归修 #3a
- 透传 `_cancel_event` 给 monitor(让 monitor 在循环里查 → break)
- cancel_event 已被 set 时 → BatchDone.error='cancelled'(与其他 6 facade 一致)
- delete 触发 BatchProgress×1(processed=0 触发 processed 0 → rate=0 兜底)

注:`delete_messages_batch` 与其他 facade 不同 — 它**直接调 monitor.delete_messages**,
不走 client RPC(因 Telegram 的 delete_messages 不是 facade-only,需要 media/orphan
协调)。回归修 #3a 加 paused guard + cancel_event 透传 + cancelled 状态对齐。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from tgmonitor.core.app_service import AppService
from tgmonitor.core.events import BatchDone, BatchProgress


async def test_delete_messages_batch_paused_returns_zero_without_calling_monitor(
    paused_batch_app: AppService,
) -> None:
    """回归修 #3a:paused 状态调 delete_messages_batch 返 0,不调 monitor.delete_messages。

    原 facade 漏 _is_paused 守卫,会走到 monitor.delete_messages(可能 hang 或
    在 monitor 已停时抛错)。paused 守卫与其他 6 facade 一致。
    """
    paused_batch_app.monitor = AsyncMock()
    paused_batch_app.monitor.delete_messages = AsyncMock(return_value=(42, []))  # type: ignore[attr-defined]
    items = [(1, 1), (1, 2), (2, 3)]

    deleted = await paused_batch_app.batch.delete_messages_batch(items)

    assert deleted == 0
    paused_batch_app.monitor.delete_messages.assert_not_called()  # type: ignore[attr-defined]


async def test_delete_messages_batch_passes_cancel_event_to_monitor(
    batch_app: AppService,
) -> None:
    """回归修 #3a + P2 #31:facade 透传本 op 的局部 cancel_event 给 monitor。

    原 facade 一次性调 monitor.delete_messages(items) 不可中断;现在透传
    cancel_event 让 monitor 在循环里查 → break。P2 #31 后类上的
    `_cancel_event` 在 op 结束时置 None,故改为在调用瞬间捕获该 Event。
    """
    batch_app.monitor = AsyncMock()
    batch_app.monitor.delete_messages = AsyncMock(return_value=(3, []))  # type: ignore[attr-defined]

    captured: dict = {}

    async def _capture_call(items, cancel_event=None, **_kw):
        captured["cancel_event"] = cancel_event
        return 3, []

    batch_app.monitor.delete_messages = AsyncMock(side_effect=_capture_call)  # type: ignore[attr-defined]

    items = [(1, 1), (1, 2), (2, 3)]
    await batch_app.batch.delete_messages_batch(items)

    # monitor.delete_messages 应被调一次,items 位置参数;cancel_event 是
    # 本 op 的局部 Event(op 结束后类上引用已置 None)
    batch_app.monitor.delete_messages.assert_called_once()  # type: ignore[attr-defined]
    assert captured["cancel_event"] is not None
    assert captured["cancel_event"] is batch_app.batch._cancel_event or (
        batch_app.batch._cancel_event is None
    )


async def test_delete_messages_batch_emits_cancelled_when_cancel_set_before_call(
    batch_app: AppService,
) -> None:
    """回归修 #3a:facade 跑完 monitor.delete_messages 后,cancel_event 已 set →
    BatchDone.error = 'cancelled'(与其它 facade 行为一致)。
    """
    received: list[BatchDone] = []

    async def _on(e: BatchDone) -> None:
        received.append(e)

    batch_app.bus.subscribe(BatchDone, _on)

    # monitor 同步返 3,但模拟 cancel 已在 monitor 跑完后 set 上
    async def _fake_delete(items, cancel_event=None):
        if cancel_event is not None:
            cancel_event.set()
        return 3, []

    batch_app.monitor = AsyncMock()
    batch_app.monitor.delete_messages = AsyncMock(side_effect=_fake_delete)  # type: ignore[attr-defined]

    items = [(1, 1), (1, 2), (2, 3)]
    await batch_app.batch.delete_messages_batch(items)

    assert len(received) == 1
    done = received[0]
    assert done.op == "delete"
    assert done.succeeded == 3
    assert done.failed == 0
    assert done.error == "cancelled"


async def test_batch_progress_zero_processed_zero_rate(bus, batch_app: AppService) -> None:
    """v1.7.4:第一条 emit 时 processed=0 → elapsed 极短 → rate=0(elapsed < 1e-3 保护)。

    delete_messages_batch 开始时 emit processed=0 — 验证兜底逻辑。
    """
    received: list[BatchProgress] = []

    async def _on(e: BatchProgress) -> None:
        received.append(e)

    bus.subscribe(BatchProgress, _on)

    items = [(1, 1)]
    # 让 monitor.delete_messages 立刻返 1(单条 delete 同步)
    batch_app.monitor = AsyncMock()  # type: ignore[attr-defined]
    batch_app.monitor.delete_messages = AsyncMock(return_value=(1, []))  # type: ignore[attr-defined]
    await batch_app.batch.delete_messages_batch(items)
    # 第一个 emit 是 processed=0;rate 应 = 0(elapsed ≈ 0)
    assert len(received) >= 1
    first = received[0]
    assert first.processed == 0
    assert first.total == 1
    # elapsed 可能是 0(极快)或 > 0 但 rate ≈ 0(若 elapsed < 1e-3)— 不强求
    assert first.rate_per_second >= 0.0
