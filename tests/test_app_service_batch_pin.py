"""2026-09-17 PR 4 mega-split:`AppService.pin_messages` / `unpin_messages` 测试。

8 tests:
- pin (4):per-cid grouping / empty noop / paused guard / BatchProgress+Done
- unpin (3):empty noop / paused guard / op='unpin' 区分
- 1 batch_progress_elapsed_rate for pin(详见 _cancel 文件)
"""

from __future__ import annotations

from tgmonitor.core.app_service import AppService
from tgmonitor.core.events import BatchDone, BatchProgress


async def test_pin_groups_by_cid(app: AppService, collected: list) -> None:
    """pin_messages 按 cid group — 1 个 RPC 每个 cid。"""
    items = [(1, 10), (2, 200), (1, 11), (2, 201), (1, 12)]
    n = await app.pin_messages(items)
    assert n == 5
    assert app.client.pin_messages.await_count == 2  # type: ignore[attr-defined]
    # 第一次调用 cid=1, mids 长度 3
    call0 = app.client.pin_messages.await_args_list[0]  # type: ignore[attr-defined]
    assert call0.args[0] in (1, 2)
    assert len(call0.args[1]) == 3


async def test_pin_empty_noop(app: AppService, collected: list) -> None:
    n = await app.pin_messages([])
    assert n == 0
    app.client.pin_messages.assert_not_awaited()  # type: ignore[attr-defined]
    assert collected == []


async def test_pin_paused_guard(paused_app: AppService, collected: list) -> None:
    n = await paused_app.pin_messages([(1, 10)])
    assert n == 0
    paused_app.client.pin_messages.assert_not_awaited()  # type: ignore[attr-defined]
    assert collected == []


async def test_pin_emits_progress_and_done(
    app: AppService, collected: list
) -> None:
    items = [(1, i) for i in range(3)]
    await app.pin_messages(items)
    progress = [e for e in collected if isinstance(e, BatchProgress)]
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert len(progress) == 1
    assert progress[0].op == "pin"
    assert progress[0].processed == 3
    assert len(done) == 1
    assert done[0].op == "pin"
    assert done[0].succeeded == 3


async def test_unpin_empty_noop(app: AppService, collected: list) -> None:
    n = await app.unpin_messages([])
    assert n == 0
    app.client.unpin_messages.assert_not_awaited()  # type: ignore[attr-defined]


async def test_unpin_paused_guard(paused_app: AppService, collected: list) -> None:
    n = await paused_app.unpin_messages([(1, 10)])
    assert n == 0
    assert collected == []


async def test_unpin_emits_done_with_correct_op(
    app: AppService, collected: list
) -> None:
    """unpin 用 op='unpin' 区分 pin。"""
    await app.unpin_messages([(1, 10), (2, 20)])
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert done[0].op == "unpin"
    assert done[0].succeeded == 2