"""2026-09-09 v1.7.2:AppService 5 个新 batch facade + 4 个元数据 facade 测试。

覆盖:
- forward / pin / unpin / add_reaction / remove_reaction
  - paused guard(直接返 0 + 不调 client)
  - per-cid grouping(混合 cid 调用,client 收到对应 cid 分组)
  - 异常隔离(单条失败不影响其他)
  - 成功计数 + empty noop(空 list / 空 emoji)
  - BatchProgress / BatchDone event emit(订阅 Bus 验证事件次数 / op / 计数)
- set_favorite / set_tags / set_notes
  - 写本地 storage + publish MessageEdited(让 UI LIVE row 刷新 ★)
  - get_message 失败/不存在时不 publish MessageEdited
- list_favorites / list_by_tag — 透传到 storage

模式:用 AsyncMock 注入 client / storage,EventBus 真订阅,验证 emit 序列。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tgmonitor.core.app_service import AppService
from tgmonitor.core.dto import MessageDTO
from tgmonitor.core.events import (
    BatchDone,
    BatchProgress,
    EventBus,
    MessageEdited,
)

# ============== fixtures ==============


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
async def app(bus: EventBus) -> AppService:
    """最小可用的 AppService — AsyncMock client / storage / objects。"""
    client = AsyncMock()
    client.state = "phone_required"
    # 默认所有 RPC 成功(无 exception)
    storage = AsyncMock()
    objects = AsyncMock()
    objects.backend_name = "fake"
    settings = MagicMock()
    settings.media_policy = "full"
    svc = AppService(bus, client, storage, objects, settings)
    # 默认不暂停
    svc._is_paused = False  # type: ignore[attr-defined]
    return svc


@pytest.fixture
async def paused_app(bus: EventBus) -> AppService:
    """暂停态 AppService — paused guard 走 short-circuit。"""
    client = AsyncMock()
    client.state = "phone_required"
    storage = AsyncMock()
    objects = AsyncMock()
    objects.backend_name = "fake"
    settings = MagicMock()
    settings.media_policy = "full"
    svc = AppService(bus, client, storage, objects, settings)
    svc._is_paused = True  # type: ignore[attr-defined]
    return svc


@pytest.fixture
def collected(bus: EventBus) -> list:
    """订阅 BatchProgress + BatchDone + MessageEdited,事件 append 到 list。"""
    out: list = []

    async def _save(evt: object) -> None:
        out.append(evt)

    bus.subscribe(BatchProgress, _save)
    bus.subscribe(BatchDone, _save)
    bus.subscribe(MessageEdited, _save)
    return out


# ============== forward_messages ==============


@pytest.mark.asyncio
async def test_forward_groups_by_cid_and_chunks_100(app: AppService, collected: list) -> None:
    """混合 cid items → client.forward_messages 按 cid group,TDLib 限 100/批。

    混合 3 条 cid=1 + 2 条 cid=2 → client 收到 2 次 forward_messages 调用,
    each 调 sorted msg_ids。
    """
    items = [(1, 10), (2, 200), (1, 11), (1, 12), (2, 201)]
    n = await app.forward_messages(items, to_chat_id=99)
    assert n == 5
    # 2 次 RPC(每个 cid 一次)
    assert app.client.forward_messages.await_count == 2  # type: ignore[attr-defined]
    # 第一次调用 — cid=1, mids=[10, 11, 12]
    call0 = app.client.forward_messages.await_args_list[0]  # type: ignore[attr-defined]
    assert call0.args == (1, 99, [10, 11, 12])
    # 第二次调用 — cid=2, mids=[200, 201](顺序由 dict 决定,只校验长度)
    call1 = app.client.forward_messages.await_args_list[1]  # type: ignore[attr-defined]
    assert call1.args[0] == 2
    assert call1.args[1] == 99
    assert sorted(call1.args[2]) == [200, 201]


@pytest.mark.asyncio
async def test_forward_empty_noop(app: AppService, collected: list) -> None:
    """空 items → 返 0,不发任何 BatchProgress / BatchDone。"""
    n = await app.forward_messages([], to_chat_id=99)
    assert n == 0
    app.client.forward_messages.assert_not_awaited()  # type: ignore[attr-defined]
    assert collected == []


@pytest.mark.asyncio
async def test_forward_paused_guard(paused_app: AppService, collected: list) -> None:
    """paused 状态 → 返 0,client 完全不被调,无 events。"""
    n = await paused_app.forward_messages([(1, 10), (1, 11)], to_chat_id=99)
    assert n == 0
    paused_app.client.forward_messages.assert_not_awaited()  # type: ignore[attr-defined]
    assert collected == []


@pytest.mark.asyncio
async def test_forward_emits_progress_and_done(app: AppService, collected: list) -> None:
    """forward 1 个 cid 5 条 → BatchProgress×N + BatchDone×1,counts 准确。"""
    items = [(1, i) for i in range(5)]
    await app.forward_messages(items, to_chat_id=99)
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


# ============== pin_messages ==============


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_pin_empty_noop(app: AppService, collected: list) -> None:
    n = await app.pin_messages([])
    assert n == 0
    app.client.pin_messages.assert_not_awaited()  # type: ignore[attr-defined]
    assert collected == []


@pytest.mark.asyncio
async def test_pin_paused_guard(paused_app: AppService, collected: list) -> None:
    n = await paused_app.pin_messages([(1, 10)])
    assert n == 0
    paused_app.client.pin_messages.assert_not_awaited()  # type: ignore[attr-defined]
    assert collected == []


@pytest.mark.asyncio
async def test_pin_emits_progress_and_done(app: AppService, collected: list) -> None:
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


# ============== unpin_messages ==============


@pytest.mark.asyncio
async def test_unpin_empty_noop(app: AppService, collected: list) -> None:
    n = await app.unpin_messages([])
    assert n == 0
    app.client.unpin_messages.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_unpin_paused_guard(paused_app: AppService, collected: list) -> None:
    n = await paused_app.unpin_messages([(1, 10)])
    assert n == 0
    assert collected == []


@pytest.mark.asyncio
async def test_unpin_emits_done_with_correct_op(app: AppService, collected: list) -> None:
    """unpin 用 op='unpin' 区分 pin。"""
    await app.unpin_messages([(1, 10), (2, 20)])
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert done[0].op == "unpin"
    assert done[0].succeeded == 2


# ============== add_reaction ==============


@pytest.mark.asyncio
async def test_add_reaction_per_message_loop(app: AppService, collected: list) -> None:
    """add_reaction 走 per-msg 循环(不是 batch RPC),N 条 → N 次调用。"""
    items = [(1, 10), (1, 11), (2, 200)]
    n = await app.add_reaction(items, "🔥")
    assert n == 3
    assert app.client.add_reaction.await_count == 3  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_add_reaction_passes_is_big_kwarg(app: AppService, collected: list) -> None:
    """add_reaction(items, emoji, is_big=True) → client 收到 is_big=True。"""
    await app.add_reaction([(1, 10)], "❤", is_big=True)
    kwargs = app.client.add_reaction.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["is_big"] is True


@pytest.mark.asyncio
async def test_add_reaction_empty_emoji_noop(app: AppService, collected: list) -> None:
    """空 emoji → 返 0,不调 client。"""
    n = await app.add_reaction([(1, 10)], "")
    assert n == 0
    app.client.add_reaction.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_add_reaction_paused_guard(paused_app: AppService, collected: list) -> None:
    n = await paused_app.add_reaction([(1, 10)], "🔥")
    assert n == 0
    paused_app.client.add_reaction.assert_not_awaited()  # type: ignore[attr-defined]
    assert collected == []


@pytest.mark.asyncio
async def test_add_reaction_emits_progress_and_done(app: AppService, collected: list) -> None:
    """add_reaction 3 条 → BatchProgress×3(逐条)+ BatchDone×1。"""
    await app.add_reaction([(1, 10), (1, 11), (2, 20)], "👍")
    progress = [e for e in collected if isinstance(e, BatchProgress)]
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert len(progress) == 3
    assert [p.processed for p in progress] == [1, 2, 3]
    assert done[0].op == "react"
    assert done[0].succeeded == 3


# ============== remove_reaction ==============


@pytest.mark.asyncio
async def test_remove_reaction_empty_noop(app: AppService, collected: list) -> None:
    n = await app.remove_reaction([], "🔥")
    assert n == 0
    app.client.remove_reaction.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_remove_reaction_emits_done_op_unreact(app: AppService, collected: list) -> None:
    await app.remove_reaction([(1, 10)], "🔥")
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert done[0].op == "unreact"
    assert done[0].succeeded == 1


# ============== 异常隔离 ==============


@pytest.mark.asyncio
async def test_add_reaction_exception_isolation(app: AppService, collected: list) -> None:
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


# ============== 元数据: set_favorite / set_tags / set_notes ==============


@pytest.mark.asyncio
async def test_set_favorite_writes_storage_and_publishes(app: AppService, collected: list) -> None:
    """set_favorite → 调 storage.set_favorite + get_message + publish MessageEdited。"""
    msg = MessageDTO(id=1, channel_id=1, telegram_msg_id=10, text="hi", is_favorite=True)
    app._storage.get_message = AsyncMock(return_value=msg)  # type: ignore[attr-defined]

    await app.set_favorite(1, 10, True)

    app._storage.set_favorite.assert_awaited_once_with(1, 10, True)  # type: ignore[attr-defined]
    edited = [e for e in collected if isinstance(e, MessageEdited)]
    assert len(edited) == 1
    assert edited[0].message.is_favorite is True


@pytest.mark.asyncio
async def test_set_favorite_message_missing_no_publish(app: AppService, collected: list) -> None:
    """get_message 返 None → 不 publish MessageEdited(避免 UI 误刷新)。"""
    app._storage.get_message = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    await app.set_favorite(1, 10, True)
    assert [e for e in collected if isinstance(e, MessageEdited)] == []


@pytest.mark.asyncio
async def test_set_tags_writes_storage_and_publishes(app: AppService, collected: list) -> None:
    msg = MessageDTO(id=1, channel_id=1, telegram_msg_id=10, text="x", tags=["tech", "news"])
    app._storage.get_message = AsyncMock(return_value=msg)  # type: ignore[attr-defined]
    await app.set_tags(1, 10, ["tech", "news"])
    app._storage.set_tags.assert_awaited_once_with(1, 10, ["tech", "news"])  # type: ignore[attr-defined]
    edited = [e for e in collected if isinstance(e, MessageEdited)]
    assert edited[0].message.tags == ["tech", "news"]


@pytest.mark.asyncio
async def test_set_notes_writes_storage_and_publishes(app: AppService, collected: list) -> None:
    msg = MessageDTO(id=1, channel_id=1, telegram_msg_id=10, text="x", notes="later")
    app._storage.get_message = AsyncMock(return_value=msg)  # type: ignore[attr-defined]
    await app.set_notes(1, 10, "later")
    app._storage.set_notes.assert_awaited_once_with(1, 10, "later")  # type: ignore[attr-defined]
    edited = [e for e in collected if isinstance(e, MessageEdited)]
    assert edited[0].message.notes == "later"


@pytest.mark.asyncio
async def test_list_favorites_delegates_to_storage(
    app: AppService,
) -> None:
    """list_favorites 透传到 storage.list_favorites,不改值。"""
    expected = [MessageDTO(id=1, channel_id=1, telegram_msg_id=10, text="x")]
    app._storage.list_favorites = AsyncMock(return_value=expected)  # type: ignore[attr-defined]
    result = await app.list_favorites()
    assert result is expected


@pytest.mark.asyncio
async def test_list_by_tag_delegates_to_storage(
    app: AppService,
) -> None:
    """list_by_tag 透传到 storage.list_by_tag(tag)。"""
    expected = [MessageDTO(id=1, channel_id=1, telegram_msg_id=10, text="x")]
    app._storage.list_by_tag = AsyncMock(return_value=expected)  # type: ignore[attr-defined]
    result = await app.list_by_tag("tech")
    app._storage.list_by_tag.assert_awaited_once_with("tech")  # type: ignore[attr-defined]
    assert result is expected


# ============================================================
# 2026-09-10 v1.7.3:批量操作取消路径
# ============================================================


@pytest.mark.asyncio
async def test_cancel_current_batch_sets_event(
    app: AppService,
) -> None:
    """v1.7.3:`cancel_current_batch()` 必须 set `_cancel_event`。"""
    assert not app._cancel_event.is_set()  # type: ignore[attr-defined]
    app.cancel_current_batch()
    assert app._cancel_event.is_set()  # type: ignore[attr-defined]
    # 多次调用安全(再次 set 已 set 的 Event)
    app.cancel_current_batch()
    assert app._cancel_event.is_set()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cancel_marks_read_stops_mid_loop(app: AppService, collected: list) -> None:
    """v1.7.3:`mark_messages_read` 取消 — break 后 stop 调 client。"""
    # 3 cid 分组各 1 条。cancel 在 cid 1 RPC 完成后触发 → cid 2 起被 break。
    items = [(1, 1), (2, 2), (3, 3)]
    call_count = 0

    async def _side_effect(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:  # 第 1 次 RPC 返回后触发 cancel
            app.cancel_current_batch()

    app.client.mark_messages_read.side_effect = _side_effect  # type: ignore[attr-defined]
    result = await app.mark_messages_read(items)
    # cid 1 RPC 已发出 → success += 1;cid 2 起 is_set → break
    assert result == 1
    assert call_count == 1
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert len(done) == 1
    assert done[0].op == "mark_read"
    assert done[0].succeeded == 1
    assert done[0].failed == 2
    assert done[0].error == "cancelled"


@pytest.mark.asyncio
async def test_cancel_pin_stops_mid_loop(app: AppService, collected: list) -> None:
    """v1.7.3:`pin_messages` 取消 — break 后 stop 调 client。"""
    items = [(1, 1), (2, 2), (3, 3)]
    call_count = 0

    async def _side_effect(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            app.cancel_current_batch()

    app.client.pin_messages.side_effect = _side_effect  # type: ignore[attr-defined]
    result = await app.pin_messages(items)
    assert result == 1
    assert call_count == 1
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert len(done) == 1
    assert done[0].op == "pin"
    assert done[0].succeeded == 1
    assert done[0].error == "cancelled"


@pytest.mark.asyncio
async def test_cancel_react_stops_mid_loop(app: AppService, collected: list) -> None:
    """v1.7.3:`add_reaction` 取消 — break 后 stop 调 client。"""
    items = [(1, 10), (1, 11), (1, 12), (1, 13)]
    call_count = 0

    async def _side_effect(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            app.cancel_current_batch()

    app.client.add_reaction.side_effect = _side_effect  # type: ignore[attr-defined]
    result = await app.add_reaction(items, "🔥")
    assert result == 1  # 第 1 条 RPC 已发出
    assert call_count == 1
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert len(done) == 1
    assert done[0].op == "react"
    assert done[0].succeeded == 1
    assert done[0].failed == 3
    assert done[0].error == "cancelled"


@pytest.mark.asyncio
async def test_cancel_after_completion_no_error(
    app: AppService, collected: list
) -> None:
    """v1.7.3:批量全完成后 cancel — 无副作用,BatchDone.error is None。"""
    items = [(1, 1), (1, 2)]
    await app.mark_messages_read(items)
    # 已 set 的 event 不影响已完成的 facade;此 facade 内部 clear → 全过完
    app.cancel_current_batch()
    # clear 由下次 facade 进入时做;此处直接看最后一次 BatchDone
    done = [e for e in collected if isinstance(e, BatchDone)]
    assert done[-1].error is None
    assert done[-1].succeeded == 2


@pytest.mark.asyncio
async def test_multiple_cancel_calls_safe(app: AppService) -> None:
    """v1.7.3:多次 cancel_current_batch 调用安全 — Event 多次 set。"""
    app.cancel_current_batch()
    app.cancel_current_batch()
    app.cancel_current_batch()
    assert app._cancel_event.is_set()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cancel_after_event_cleared_by_next_facade(
    app: AppService, collected: list
) -> None:
    """v1.7.3:facade 开头 `_cancel_event.clear()` — 上次 cancel 不影响下次。"""
    # 第 1 次 facade:cancel after first RPC
    items1 = [(1, 1), (2, 2), (3, 3)]
    call_count = 0

    async def _side_effect(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            app.cancel_current_batch()

    app.client.mark_messages_read.side_effect = _side_effect  # type: ignore[attr-defined]
    await app.mark_messages_read(items1)
    done1 = [e for e in collected if isinstance(e, BatchDone)]
    assert done1[0].error == "cancelled"
    assert done1[0].succeeded == 1

    # clear collected
    collected.clear()
    # 解除 side_effect → 默认 AsyncMock 不抛
    app.client.mark_messages_read.side_effect = None  # type: ignore[attr-defined]

    # 第 2 次 facade 不应被上次的 event 影响(开头 clear)
    items2 = [(1, 100), (1, 101)]
    await app.mark_messages_read(items2)
    done2 = [e for e in collected if isinstance(e, BatchDone)]
    assert done2[0].error is None
    assert done2[0].succeeded == 2
