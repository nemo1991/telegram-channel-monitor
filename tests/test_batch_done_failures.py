"""2026-09-17 PR 4:`BatchDone.failures` 字段契约测试。

PR 4 plan:`failures: list[tuple[int, int, str]]` 列出每条失败的 `(cid, mid,
error_str)` 三元组 — 给「查看失败详情」按钮弹 dialog 用(v1.7.5 P0-J)。

7 个 batch op × 1 failure-isolation 测试:
- `delete_messages_batch` — 走 `monitor.delete_messages`
- `mark_messages_read` — 走 `client.mark_messages_read` (per-cid grouping)
- `forward_messages` — 走 `client.forward_messages` (per-cid grouping)
- `pin_messages` — 走 `client.pin_messages` (per-cid grouping)
- `unpin_messages` — 走 `client.unpin_messages` (per-cid grouping)
- `add_reaction` — 走 `client.add_reaction` (per-msg,emoji 是 op-level arg)
- `remove_reaction` — 走 `client.remove_reaction` (per-msg,emoji 是 op-level arg)

契约:
- 单 op 失败 → 对应 entry 在 `failures`,其他成功的不在
- 错误原因用 `str(exc) or exc.__class__.__name__` 序列化
- `failed` 字段 = `len(failures)`,`succeeded` = 成功条数
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from tgmonitor.core.app_service import AppService
from tgmonitor.core.events import BatchDone, EventBus
from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.fake_client import FakeTelegramClient

# ============== helpers ==============


def _make_app(bus: EventBus) -> AppService:
    """最小可用 AppService + 真 MonitorService + 真 FakeTelegramClient。"""
    client = FakeTelegramClient()
    storage = MagicMock(spec=StorageRepository)
    storage.connect = AsyncMock()
    storage.init_schema = AsyncMock()
    storage.list_subscribed_channels = AsyncMock(return_value=[])
    storage.list_messages = AsyncMock(return_value=[])
    storage.delete_messages = AsyncMock(return_value=2)
    objects = MagicMock()
    objects.backend_name = "fake"
    settings = MagicMock()
    settings.media_policy = "metadata"
    mon = MonitorService(bus, client, storage, objects, settings)
    svc = AppService(bus, client, storage, objects, settings, monitor=mon)
    svc._is_paused = False  # type: ignore[attr-defined]
    return svc


def _capture_batch_done(bus: EventBus) -> list[BatchDone]:
    """订阅 BatchDone,收集事件。"""
    out: list[BatchDone] = []
    bus.subscribe(BatchDone, lambda e: out.append(e))
    return out


# ============== delete_messages_batch ==============


async def test_batch_done_failures_delete_messages(bus: EventBus) -> None:
    """`delete_messages_batch` 部分失败 → `failures` 列出每条 `(cid, mid, reason)`。

    `monitor.delete_messages` 是 monitor 内部 SQL;失败时整批 try/except 兜底。
    """
    svc = _make_app(bus)
    # 替换 monitor.delete_messages,模拟部分失败
    # signature: delete_messages(items, cancel_event=None) -> int
    svc.monitor.delete_messages = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("db locked")
    )
    captured = _capture_batch_done(bus)

    items = [(100, 1), (100, 2), (200, 3)]
    await svc.delete_messages_batch(items)

    assert len(captured) == 1
    done = captured[0]
    assert done.op == "delete"
    assert done.failed == 3
    assert done.succeeded == 0
    # 失败明细
    assert len(done.failures) == 3
    assert (100, 1, "db locked") in done.failures
    assert (100, 2, "db locked") in done.failures
    assert (200, 3, "db locked") in done.failures


# ============== mark_messages_read ==============


async def test_batch_done_failures_mark_messages_read(bus: EventBus) -> None:
    """`mark_messages_read` 单 cid 失败 → 该 cid 的所有 mid 列入 failures。"""
    svc = _make_app(bus)
    # 替换 client.mark_messages_read:cid=100 抛,cid=200 成功
    async def _side_effect(cid: int, msg_ids: list[int]) -> None:
        if cid == 100:
            raise RuntimeError("mark_read 100 failed")

    svc.client.mark_messages_read = AsyncMock(side_effect=_side_effect)  # type: ignore[attr-defined]
    captured = _capture_batch_done(bus)

    items = [(100, 1), (100, 2), (200, 3), (200, 4)]
    await svc.mark_messages_read(items)

    assert len(captured) == 1
    done = captured[0]
    assert done.op == "mark_read"
    assert done.failed == 2
    assert done.succeeded == 2
    # cid=100 的两条失败
    assert (100, 1, "mark_read 100 failed") in done.failures
    assert (100, 2, "mark_read 100 failed") in done.failures
    # cid=200 的两条成功 — 不在 failures
    assert (200, 3, "...") not in done.failures
    assert (200, 4, "...") not in done.failures


# ============== forward_messages ==============


async def test_batch_done_failures_forward_messages(bus: EventBus) -> None:
    """`forward_messages` per-cid 失败 → failures 列该 cid 全 mid。"""
    svc = _make_app(bus)

    async def _side_effect(from_cid: int, to_cid: int, msg_ids: list[int]) -> None:
        if from_cid == 100:
            raise RuntimeError("forward 100 denied")

    svc.client.forward_messages = AsyncMock(side_effect=_side_effect)  # type: ignore[attr-defined]
    captured = _capture_batch_done(bus)

    items = [(100, 1), (100, 2), (200, 3)]
    await svc.forward_messages(items, to_chat_id=999)

    assert len(captured) == 1
    done = captured[0]
    assert done.op == "forward"
    assert done.failed == 2
    assert done.succeeded == 1
    assert (100, 1, "forward 100 denied") in done.failures
    assert (100, 2, "forward 100 denied") in done.failures


# ============== pin_messages ==============


async def test_batch_done_failures_pin_messages(bus: EventBus) -> None:
    """`pin_messages` per-cid 失败。"""
    svc = _make_app(bus)

    async def _side_effect(cid: int, msg_ids: list[int], **kw) -> None:
        if cid == 100:
            raise RuntimeError("pin 100 forbidden")

    svc.client.pin_messages = AsyncMock(side_effect=_side_effect)  # type: ignore[attr-defined]
    captured = _capture_batch_done(bus)

    items = [(100, 1), (200, 2), (200, 3)]
    await svc.pin_messages(items)

    assert len(captured) == 1
    done = captured[0]
    assert done.op == "pin"
    assert done.failed == 1
    assert done.succeeded == 2
    assert (100, 1, "pin 100 forbidden") in done.failures


# ============== unpin_messages ==============


async def test_batch_done_failures_unpin_messages(bus: EventBus) -> None:
    """`unpin_messages` per-cid 失败。"""
    svc = _make_app(bus)

    async def _side_effect(cid: int, msg_ids: list[int]) -> None:
        if cid == 200:
            raise RuntimeError("unpin 200 error")

    svc.client.unpin_messages = AsyncMock(side_effect=_side_effect)  # type: ignore[attr-defined]
    captured = _capture_batch_done(bus)

    items = [(100, 1), (200, 2)]
    await svc.unpin_messages(items)

    assert len(captured) == 1
    done = captured[0]
    assert done.op == "unpin"
    assert done.failed == 1
    assert done.succeeded == 1
    assert (200, 2, "unpin 200 error") in done.failures


# ============== add_reaction ==============


async def test_batch_done_failures_add_reaction(bus: EventBus) -> None:
    """`add_reaction` per-msg 失败 — 单条 failure。emoji 是 op-level 第二参。

    items 是 `(cid, mid)` 对(没 emoji);emoji 通过 `add_reaction(items, emoji)`
    单独传。每条单独调 `client.add_reaction(cid, mid, emoji, ...)`。
    """
    svc = _make_app(bus)

    async def _side_effect(cid: int, msg_id: int, reaction: str, **kw) -> None:
        if cid == 100 and msg_id == 2:
            raise RuntimeError("reaction 100/2 denied")

    svc.client.add_reaction = AsyncMock(side_effect=_side_effect)  # type: ignore[attr-defined]
    captured = _capture_batch_done(bus)

    items = [(100, 1), (100, 2), (200, 3)]
    await svc.add_reaction(items, emoji="🔥")

    assert len(captured) == 1
    done = captured[0]
    assert done.op == "react"
    assert done.failed == 1
    assert done.succeeded == 2
    assert (100, 2, "reaction 100/2 denied") in done.failures


# ============== remove_reaction ==============


async def test_batch_done_failures_remove_reaction(bus: EventBus) -> None:
    """`remove_reaction` per-msg 失败 — 单条 failure。emoji 是 op-level 第二参。"""
    svc = _make_app(bus)

    async def _side_effect(cid: int, msg_id: int, reaction: str) -> None:
        if cid == 200 and msg_id == 5:
            raise RuntimeError("remove_reaction 200/5 error")

    svc.client.remove_reaction = AsyncMock(side_effect=_side_effect)  # type: ignore[attr-defined]
    captured = _capture_batch_done(bus)

    items = [(100, 1), (200, 5), (200, 6)]
    await svc.remove_reaction(items, emoji="👍")

    assert len(captured) == 1
    done = captured[0]
    assert done.op == "unreact"
    assert done.failed == 1
    assert done.succeeded == 2
    assert (200, 5, "remove_reaction 200/5 error") in done.failures


# ============== 空 str exc → 用 class name 兜底 ==============


async def test_batch_done_failures_uses_class_name_when_str_empty(
    bus: EventBus,
) -> None:
    """`str(exc)` 空字符串时(某些 C 扩展异常)→ 用 `exc.__class__.__name__` 兜底。"""
    svc = _make_app(bus)

    class EmptyStrError(RuntimeError):
        def __str__(self) -> str:
            return ""

    async def _side_effect(cid: int, msg_ids: list[int], **kw) -> None:
        raise EmptyStrError()

    svc.client.pin_messages = AsyncMock(side_effect=_side_effect)  # type: ignore[attr-defined]
    captured = _capture_batch_done(bus)

    items = [(100, 1)]
    await svc.pin_messages(items)

    assert len(captured) == 1
    # 失败 reason 用类名
    assert captured[0].failures == [(100, 1, "EmptyStrError")]