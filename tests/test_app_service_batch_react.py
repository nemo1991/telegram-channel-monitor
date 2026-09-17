"""2026-09-17 PR 4 mega-split:`AppService.add_reaction` / `remove_reaction` 测试。

8 tests:
- add_reaction (5):per-msg loop / is_big kwarg / empty emoji noop / paused / BatchProgress+Done
- remove_reaction (2):empty noop / op='unreact' 区分
- 异常隔离 (1):单条失败不影响其他(succeeded=2, failed=1)

注:`add_reaction` 走 per-msg 循环(不是 batch RPC),N 条 → N 次 client 调用;
`remove_reaction` 同。
"""

from __future__ import annotations

from tgmonitor.core.app_service import AppService
from tgmonitor.core.events import BatchDone, BatchProgress


async def test_add_reaction_per_message_loop(
    app: AppService, collected: list
) -> None:
    """add_reaction 走 per-msg 循环(不是 batch RPC),N 条 → N 次调用。"""
    items = [(1, 10), (1, 11), (2, 200)]
    n = await app.add_reaction(items, "🔥")
    assert n == 3
    assert app.client.add_reaction.await_count == 3  # type: ignore[attr-defined]


async def test_add_reaction_passes_is_big_kwarg(
    app: AppService, collected: list
) -> None:
    """add_reaction(items, emoji, is_big=True) → client 收到 is_big=True。"""
    await app.add_reaction([(1, 10)], "❤", is_big=True)
    kwargs = app.client.add_reaction.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["is_big"] is True


async def test_add_reaction_empty_emoji_noop(
    app: AppService, collected: list
) -> None:
    """空 emoji → 返 0,不调 client。"""
    n = await app.add_reaction([(1, 10)], "")
    assert n == 0
    app.client.add_reaction.assert_not_awaited()  # type: ignore[attr-defined]


async def test_add_reaction_paused_guard(
    paused_app: AppService, collected: list
) -> None:
    n = await paused_app.add_reaction([(1, 10)], "🔥")
    assert n == 0
    paused_app.client.add_reaction.assert_not_awaited()  # type: ignore[attr-defined]
    assert collected == []


async def test_add_reaction_emits_progress_and_done(
    app: AppService, collected: list
) -> None:
    """add_reaction 3 条 → BatchProgress×3(逐条)+ BatchDone×1。"""
    await app.add_reaction([(1, 10), (1, 11), (2, 20)], "👍")
    progress = [e for e in collected if isinstance(e, BatchProgress)]
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert len(progress) == 3
    assert [p.processed for p in progress] == [1, 2, 3]
    assert done[0].op == "react"
    assert done[0].succeeded == 3


async def test_remove_reaction_empty_noop(
    app: AppService, collected: list
) -> None:
    n = await app.remove_reaction([], "🔥")
    assert n == 0
    app.client.remove_reaction.assert_not_awaited()  # type: ignore[attr-defined]


async def test_remove_reaction_emits_done_op_unreact(
    app: AppService, collected: list
) -> None:
    await app.remove_reaction([(1, 10)], "🔥")
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert done[0].op == "unreact"
    assert done[0].succeeded == 1


async def test_add_reaction_exception_isolation(
    app: AppService, collected: list
) -> None:
    """add_reaction 第 2 条抛错 — 第 1 / 第 3 仍执行,success=2,failed=1。

    这是 per-msg 循环异常隔离的兜底 — 一条 emoji reaction 不应阻断其余。
    """
    call_count = {"n": 0}

    async def maybe_fail(cid: int, mid: int, emoji: str, *, is_big: bool = False) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated TDlib error")

    app.client.add_reaction.side_effect = maybe_fail  # type: ignore[attr-defined]

    n = await app.add_reaction([(1, 10), (1, 11), (1, 12)], "🔥")
    assert n == 2  # 第 2 条失败
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert done[0].succeeded == 2
    assert done[0].failed == 1