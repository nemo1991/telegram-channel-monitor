"""BatchService — 批量操作域服务(7 op + 取消 + ETA 进度)。

2026-11-11 v2.0.0 Phase 3:从 `app_service/_batch_ops.py`(module-level
function 持 facade mutable state)改为独立 class,**自持**取消 Event +
批量计时起点 — 不再与 facade 共享 `_cancel_event` / `_batch_started_at`。

7 batch op(UI 批量多选触发):
- `delete_messages_batch` — 走 `monitor.delete_messages(items, cancel_event)`
- `mark_messages_read` — 走 `client.mark_messages_read(cid, msg_ids)`
- `forward_messages` — 走 `client.forward_messages` + `_chunks(mids, 100)`
- `pin_messages` / `unpin_messages` — 走 `client.pin_messages` / `unpin_messages`
- `add_reaction` / `remove_reaction` — 走 `client.add_reaction` / `remove_reaction`

取消语义:`cancel_current_batch()` set Event,op 循环下一个 item 前检查
break + 发 BatchDone(error='cancelled')。不调 task.cancel — 批量在主 task
loop 里 await,set Event 即可跳出;task.cancel 的 CancelledError 会被
run_coro 吞,无法发 BatchDone。已发出的 RPC server-side 仍可能完成,
best-effort。

paused 守卫:构造时注入 `is_paused: Callable[[], bool]`(读 AppService
实时状态,不持副本)— paused 期间 client 已 stop,7 op 返 0 + log warning。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Callable

from tgmonitor.core.events import BatchDone, BatchProgress, EventBus
from tgmonitor.core.telegram.client import TelegramClient

if TYPE_CHECKING:
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)


class BatchService:
    """批量消息操作 — 自持取消 Event + ETA 计时,进度走 BatchProgress/BatchDone。"""

    def __init__(
        self,
        bus: EventBus,
        client: TelegramClient,
        monitor: MonitorService | None = None,
        *,
        is_paused: Callable[[], bool] = lambda: False,
    ) -> None:
        """bus + client + monitor(仅 delete 用)+ paused 状态读取 callback。"""
        self._bus = bus
        self._client = client
        self._monitor = monitor
        self._is_paused = is_paused
        self._cancel_event: asyncio.Event = asyncio.Event()
        self._batch_started_at: float = 0.0

    # ---------- 取消 ----------

    def cancel_current_batch(self) -> None:
        """取消当前批量操作 — set Event,op 下一个 item 检查后 break。

        多次调用安全(只是 set 已 set 的 Event);无批量进行时也无副作用
        (下一次 op 开头 clear)。
        """
        self._cancel_event.set()

    # ---------- ETA helper ----------

    def _start_batch_timer(self) -> None:
        """批量开始 — 记 `time.monotonic()` 起点(每个 op 开头调一次)。"""
        self._batch_started_at = time.monotonic()

    def _compute_rate(self, processed: int) -> tuple[float, float]:
        """算 (elapsed_seconds, rate_per_second);elapsed < 1e-3 时 rate 返 0。"""
        elapsed = time.monotonic() - self._batch_started_at
        rate = processed / elapsed if elapsed > 1e-3 else 0.0
        return elapsed, rate

    async def _publish_batch_progress(self, op: str, processed: int, total: int) -> None:
        """发布 BatchProgress,自动加 elapsed_seconds / rate_per_second。"""
        elapsed, rate = self._compute_rate(processed)
        await self._bus.publish(
            BatchProgress(
                op=op,
                processed=processed,
                total=total,
                elapsed_seconds=elapsed,
                rate_per_second=rate,
            )
        )

    @staticmethod
    def _chunks(items: list[int], size: int) -> list[list[int]]:
        """把 list 切成 ≤size 的连续子列表(forward 上限 100);空 list 返 []。"""
        if not items:
            return []
        return [items[i : i + size] for i in range(0, len(items), size)]

    # ---------- 7 批量 op ----------

    async def delete_messages_batch(self, items: list[tuple[int, int]]) -> int:
        """批量删 N 条消息 — 转 MonitorService.delete_messages(带 cancel_event)。

        返成功条数;每条成功 publish MessageDeleted → LIVE view remove_row。
        monitor 内部循环不逐条 emit,进度按 (deleted, total) 末尾发一次。
        """
        if not items:
            return 0
        if self._is_paused():
            log.warning("BatchService.delete_messages_batch: paused, skip %d items", len(items))
            return 0
        assert self._monitor is not None
        self._cancel_event.clear()
        self._start_batch_timer()
        await self._publish_batch_progress("delete", 0, len(items))
        failures: list[tuple[int, int, str]] = []
        try:
            deleted = await self._monitor.delete_messages(items, cancel_event=self._cancel_event)
        except Exception as exc:  # noqa: BLE001
            log.exception("delete_messages_batch 全批失败")
            deleted = 0
            for cid, mid in items:
                failures.append((cid, mid, str(exc) or exc.__class__.__name__))
        await self._publish_batch_progress("delete", deleted, len(items))
        await self._bus.publish(
            BatchDone(
                op="delete",
                succeeded=deleted,
                failed=len(items) - deleted,
                error="cancelled" if self._cancel_event.is_set() else None,
                failures=failures,
            )
        )
        return deleted

    async def mark_messages_read(self, items: list[tuple[int, int]]) -> int:
        """批量标已读 — 按 channel_id 分组,各调一次 client.mark_messages_read。"""
        if not items:
            return 0
        if self._is_paused():
            log.warning("BatchService.mark_messages_read: paused, skip %d items", len(items))
            return 0
        self._cancel_event.clear()
        self._start_batch_timer()
        by_cid: dict[int, list[int]] = {}
        for cid, mid in items:
            by_cid.setdefault(cid, []).append(mid)
        success = 0
        cancelled = False
        failures: list[tuple[int, int, str]] = []
        for cid, msg_ids in by_cid.items():
            if self._cancel_event.is_set():
                cancelled = True
                break
            try:
                await self._client.mark_messages_read(cid, msg_ids)
                success += len(msg_ids)
                await self._publish_batch_progress("mark_read", success, len(items))
            except Exception as exc:  # noqa: BLE001
                log.exception("mark_messages_read(cid=%s) failed", cid)
                for mid in msg_ids:
                    failures.append((cid, mid, str(exc) or exc.__class__.__name__))
        await self._bus.publish(
            BatchDone(
                op="mark_read",
                succeeded=success,
                failed=len(items) - success,
                error="cancelled" if cancelled else None,
                failures=failures,
            )
        )
        return success

    async def forward_messages(self, items: list[tuple[int, int]], to_chat_id: int) -> int:
        """批量转发 — 按 from_cid group,各走 `_chunks(mids, 100)`(API 上限)。

        已发的 chunk 不撤回(超出控制范围,best-effort)。
        """
        if not items:
            return 0
        if self._is_paused():
            log.warning("BatchService.forward_messages: paused, skip %d items", len(items))
            return 0
        self._cancel_event.clear()
        self._start_batch_timer()
        by_cid: dict[int, list[int]] = {}
        for cid, mid in items:
            by_cid.setdefault(cid, []).append(mid)
        success = 0
        cancelled = False
        failures: list[tuple[int, int, str]] = []
        for from_cid, mids in by_cid.items():
            for chunk in self._chunks(mids, 100):
                if self._cancel_event.is_set():
                    cancelled = True
                    break
                try:
                    await self._client.forward_messages(from_cid, to_chat_id, chunk)
                    success += len(chunk)
                    await self._publish_batch_progress("forward", success, len(items))
                except Exception as exc:  # noqa: BLE001
                    log.exception(
                        "forward_messages(%s → %s, %d) failed", from_cid, to_chat_id, len(chunk)
                    )
                    for mid in chunk:
                        failures.append((from_cid, mid, str(exc) or exc.__class__.__name__))
            if cancelled:
                break
        await self._bus.publish(
            BatchDone(
                op="forward",
                succeeded=success,
                failed=len(items) - success,
                error="cancelled" if cancelled else None,
                failures=failures,
            )
        )
        return success

    async def pin_messages(self, items: list[tuple[int, int]]) -> int:
        """批量钉选 — 按 cid group(per-msg RPC,异常隔离粒度到单条)。"""
        if not items:
            return 0
        if self._is_paused():
            log.warning("BatchService.pin_messages: paused, skip %d items", len(items))
            return 0
        self._cancel_event.clear()
        self._start_batch_timer()
        by_cid: dict[int, list[int]] = {}
        for cid, mid in items:
            by_cid.setdefault(cid, []).append(mid)
        success = 0
        cancelled = False
        failures: list[tuple[int, int, str]] = []
        for cid, msg_ids in by_cid.items():
            if self._cancel_event.is_set():
                cancelled = True
                break
            try:
                await self._client.pin_messages(cid, msg_ids)
                success += len(msg_ids)
                await self._publish_batch_progress("pin", success, len(items))
            except Exception as exc:  # noqa: BLE001
                log.exception("pin_messages(cid=%s) failed", cid)
                for mid in msg_ids:
                    failures.append((cid, mid, str(exc) or exc.__class__.__name__))
        await self._bus.publish(
            BatchDone(
                op="pin",
                succeeded=success,
                failed=len(items) - success,
                error="cancelled" if cancelled else None,
                failures=failures,
            )
        )
        return success

    async def unpin_messages(self, items: list[tuple[int, int]]) -> int:
        """批量取消钉选 — 同 pin_messages 模式。"""
        if not items:
            return 0
        if self._is_paused():
            log.warning("BatchService.unpin_messages: paused, skip %d items", len(items))
            return 0
        self._cancel_event.clear()
        self._start_batch_timer()
        by_cid: dict[int, list[int]] = {}
        for cid, mid in items:
            by_cid.setdefault(cid, []).append(mid)
        success = 0
        cancelled = False
        failures: list[tuple[int, int, str]] = []
        for cid, msg_ids in by_cid.items():
            if self._cancel_event.is_set():
                cancelled = True
                break
            try:
                await self._client.unpin_messages(cid, msg_ids)
                success += len(msg_ids)
                await self._publish_batch_progress("unpin", success, len(items))
            except Exception as exc:  # noqa: BLE001
                log.exception("unpin_messages(cid=%s) failed", cid)
                for mid in msg_ids:
                    failures.append((cid, mid, str(exc) or exc.__class__.__name__))
        await self._bus.publish(
            BatchDone(
                op="unpin",
                succeeded=success,
                failed=len(items) - success,
                error="cancelled" if cancelled else None,
                failures=failures,
            )
        )
        return success

    async def add_reaction(
        self,
        items: list[tuple[int, int]],
        emoji: str,
        *,
        is_big: bool = False,
    ) -> int:
        """批量 emoji 回应 — per-msg RPC,每条失败 append (cid, mid, error)。"""
        if not items or not emoji:
            return 0
        if self._is_paused():
            log.warning("BatchService.add_reaction: paused, skip %d items", len(items))
            return 0
        self._cancel_event.clear()
        self._start_batch_timer()
        success = 0
        cancelled = False
        failures: list[tuple[int, int, str]] = []
        for cid, mid in items:
            if self._cancel_event.is_set():
                cancelled = True
                break
            try:
                await self._client.add_reaction(cid, mid, emoji, is_big=is_big)
                success += 1
                await self._publish_batch_progress("react", success, len(items))
            except Exception as exc:  # noqa: BLE001
                log.exception("add_reaction(%s, %s, %s) failed", cid, mid, emoji)
                failures.append((cid, mid, str(exc) or exc.__class__.__name__))
        await self._bus.publish(
            BatchDone(
                op="react",
                succeeded=success,
                failed=len(items) - success,
                error="cancelled" if cancelled else None,
                failures=failures,
            )
        )
        return success

    async def remove_reaction(self, items: list[tuple[int, int]], emoji: str) -> int:
        """批量取消 emoji 回应 — 同 add_reaction 模式。"""
        if not items or not emoji:
            return 0
        if self._is_paused():
            log.warning("BatchService.remove_reaction: paused, skip %d items", len(items))
            return 0
        self._cancel_event.clear()
        self._start_batch_timer()
        success = 0
        cancelled = False
        failures: list[tuple[int, int, str]] = []
        for cid, mid in items:
            if self._cancel_event.is_set():
                cancelled = True
                break
            try:
                await self._client.remove_reaction(cid, mid, emoji)
                success += 1
                await self._publish_batch_progress("unreact", success, len(items))
            except Exception as exc:  # noqa: BLE001
                log.exception("remove_reaction(%s, %s, %s) failed", cid, mid, emoji)
                failures.append((cid, mid, str(exc) or exc.__class__.__name__))
        await self._bus.publish(
            BatchDone(
                op="unreact",
                succeeded=success,
                failed=len(items) - success,
                error="cancelled" if cancelled else None,
                failures=failures,
            )
        )
        return success
