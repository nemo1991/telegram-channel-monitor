"""2026-09-17 PR 4 mega-split:`AppService.forward_messages` 批 facade 测试。

4 tests 覆盖:
- per-cid grouping(混合 cid → 多次 RPC,每 cid 一次)
- empty items noop(返 0 + 不调 client + 无事件)
- paused guard(短路返 0)
- BatchProgress×N + BatchDone×1(counter 准确)
"""

from __future__ import annotations

from tgmonitor.core.app_service import AppService
from tgmonitor.core.events import BatchDone, BatchProgress


async def test_forward_groups_by_cid_and_chunks_100(batch_app: AppService, collected: list) -> None:
    """混合 cid items → client.forward_messages 按 cid group,TDLib 限 100/批。

    混合 3 条 cid=1 + 2 条 cid=2 → client 收到 2 次 forward_messages 调用,
    each 调 sorted msg_ids。
    """
    items = [(1, 10), (2, 200), (1, 11), (1, 12), (2, 201)]
    n = await batch_app.forward_messages(items, to_chat_id=99)
    assert n == 5
    # 2 次 RPC(每个 cid 一次)
    assert batch_app.client.forward_messages.await_count == 2  # type: ignore[attr-defined]
    # 第一次调用 — cid=1, mids=[10, 11, 12]
    call0 = batch_app.client.forward_messages.await_args_list[0]  # type: ignore[attr-defined]
    assert call0.args == (1, 99, [10, 11, 12])
    # 第二次调用 — cid=2, mids=[200, 201](顺序由 dict 决定,只校验长度)
    call1 = batch_app.client.forward_messages.await_args_list[1]  # type: ignore[attr-defined]
    assert call1.args[0] == 2
    assert call1.args[1] == 99
    assert sorted(call1.args[2]) == [200, 201]


async def test_forward_empty_noop(batch_app: AppService, collected: list) -> None:
    """空 items → 返 0,不发任何 BatchProgress / BatchDone。"""
    n = await batch_app.forward_messages([], to_chat_id=99)
    assert n == 0
    batch_app.client.forward_messages.assert_not_awaited()  # type: ignore[attr-defined]
    assert collected == []


async def test_forward_paused_guard(paused_batch_app: AppService, collected: list) -> None:
    """paused 状态 → 返 0,client 完全不被调,无 events。"""
    n = await paused_batch_app.forward_messages([(1, 10), (1, 11)], to_chat_id=99)
    assert n == 0
    paused_batch_app.client.forward_messages.assert_not_awaited()  # type: ignore[attr-defined]
    assert collected == []


async def test_forward_emits_progress_and_done(batch_app: AppService, collected: list) -> None:
    """forward 1 个 cid 5 条 → BatchProgress×N + BatchDone×1,counts 准确。"""
    items = [(1, i) for i in range(5)]
    await batch_app.forward_messages(items, to_chat_id=99)
    # 1 chunk 5 条 → 1 BatchProgress(processed=5,total=5) + 1 BatchDone
    progress = [e for e in collected if isinstance(e, BatchProgress)]
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert len(progress) == 1
    assert progress[0].op == "forward"
    assert progress[0].processed == 5
    assert progress[0].total == 5
    assert len(done) == 1
    assert done[0].op == "forward"
    assert done[0].succeeded == 5
    assert done[0].failed == 0
