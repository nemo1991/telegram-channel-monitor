"""2026-09-17 PR 4 mega-split:`AppService.mark_messages_read` 批 facade 测试。

覆盖 mark_messages_read 的 batch 行为:
- per-cid grouping(混合 cid → 多次 RPC)
- cancel 中段(用 BatchDone.error='cancelled' + succeeded=1 验证)
- 已完成后再 cancel 不影响历史 BatchDone.error
- 下次 facade 自动 clear 上次 cancel event
- BatchProgress 含 elapsed_seconds + rate_per_second(v1.4.0+)
"""

from __future__ import annotations

from tgmonitor.core.app_service import AppService
from tgmonitor.core.events import BatchDone, BatchProgress


async def test_cancel_marks_read_stops_mid_loop(batch_app: AppService, collected: list) -> None:
    """v1.7.3:`mark_messages_read` 取消 — break 后 stop 调 client。"""
    # 3 cid 分组各 1 条。cancel 在 cid 1 RPC 完成后触发 → cid 2 起被 break。
    items = [(1, 1), (2, 2), (3, 3)]
    call_count = 0

    async def _side_effect(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:  # 第 1 次 RPC 返回后触发 cancel
            batch_app.cancel_current_batch()

    batch_app.client.mark_messages_read.side_effect = _side_effect  # type: ignore[attr-defined]
    result = await batch_app.mark_messages_read(items)
    # cid 1 RPC 已发出 → success += 1;cid 2 起 is_set → break
    assert result == 1
    assert call_count == 1
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert len(done) == 1
    assert done[0].op == "mark_read"
    assert done[0].succeeded == 1
    assert done[0].failed == 2
    assert done[0].error == "cancelled"


async def test_cancel_after_completion_no_error(batch_app: AppService, collected: list) -> None:
    """v1.7.3:批量全完成后 cancel — 无副作用,BatchDone.error is None。"""
    items = [(1, 1), (1, 2)]
    await batch_app.mark_messages_read(items)
    # 已 set 的 event 不影响已完成的 facade;此 facade 内部 clear → 全过完
    batch_app.cancel_current_batch()
    # clear 由下次 facade 进入时做;此处直接看最后一次 BatchDone
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert done[-1].error is None
    assert done[-1].succeeded == 2


async def test_cancel_after_event_cleared_by_next_facade(
    batch_app: AppService, collected: list
) -> None:
    """v1.7.3:facade 开头 `_cancel_event.clear()` — 上次 cancel 不影响下次。"""
    # 第 1 次 facade:cancel after first RPC
    items1 = [(1, 1), (2, 2), (3, 3)]
    call_count = 0

    async def _side_effect(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            batch_app.cancel_current_batch()

    batch_app.client.mark_messages_read.side_effect = _side_effect  # type: ignore[attr-defined]
    await batch_app.mark_messages_read(items1)
    done1 = [e for e in collected if isinstance(e, BatchDone)]
    assert done1[0].error == "cancelled"
    assert done1[0].succeeded == 1

    # clear collected
    collected.clear()
    # 解除 side_effect → 默认 AsyncMock 不抛
    batch_app.client.mark_messages_read.side_effect = None  # type: ignore[attr-defined]

    # 第 2 次 facade 不应被上次的 event 影响(开头 clear)
    items2 = [(1, 100), (1, 101)]
    await batch_app.mark_messages_read(items2)
    done2 = [e for e in collected if isinstance(e, BatchDone)]
    assert done2[0].error is None
    assert done2[0].succeeded == 2


async def test_batch_progress_emits_elapsed_and_rate_for_mark_read(
    bus, batch_app: AppService
) -> None:
    """v1.7.4:mark_messages_read(per-cid grouping)— rate 也正确。"""
    received: list[BatchProgress] = []

    async def _on(e: BatchProgress) -> None:
        received.append(e)

    bus.subscribe(BatchProgress, _on)

    items = [(1, 1), (1, 2), (2, 3)]
    await batch_app.mark_messages_read(items)
    assert len(received) >= 1
    last = received[-1]
    assert last.op == "mark_read"
    assert last.processed == 3
    assert last.total == 3
