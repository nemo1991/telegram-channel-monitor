"""BatchService — 批量操作域服务(6 op + 取消 + ETA 进度)。

2026-11-11 v2.0.0 Phase 3:从 `app_service/_batch_ops.py`(module-level
function 持 facade mutable state)改为独立 class,**自持**取消 Event +
批量计时起点 — 不再与 facade 共享 `_cancel_event` / `_batch_started_at`。

6 batch op(UI 批量多选触发):
- `delete_messages_batch` — 走 `monitor.delete_messages(items, cancel_event)`
- `forward_messages` — 走 `client.forward_messages` + `_chunks(mids, 100)`
- `pin_messages` / `unpin_messages` — 走 `client.pin_messages` / `unpin_messages`
- `add_reaction` / `remove_reaction` — 走 `client.add_reaction` / `remove_reaction`

取消语义(2026-11-25 P2 #31 重构):**每个 op 进入时创建局部
`asyncio.Event`**,整个执行链(delete 传给 monitor / 其余 op 内部循环)
都引用这个局部 Event;类上只保留 `_cancel_event` 指向"当前 op 的
event"供 `cancel_current_batch()` 定位,op 结束(含异常)时在 finally
置回 None。旧实现是单一 Event + 各 op 开头 clear() — 两个批量并发时,
后启动的 op 一 clear 就把前一个的取消信号抹掉。新语义:

- 并发两个 op 时,`cancel_current_batch()` 作用于**最近注册**的那个 op
  (前一个 op 的取消窗口已过,语义由 UI 单批量 dialog 前提兜底:同一时刻
  至多一个可见批量);最近注册的 op 结束后引用才清 —— 先注册的那个先
  结束不会清掉它(2.1.7 修);
- 无 op 进行时调用 `cancel_current_batch()` 是 no-op(置 None 防悬挂
  set 落到下一个 op 上 — 下一个 op 用自己的新 Event,旧 set 自然失效)。

不调 task.cancel — 批量在主 task loop 里 await,set Event 即可跳出;
task.cancel 的 CancelledError 会被 run_coro 吞,无法发 BatchDone。
已发出的 RPC server-side 仍可能完成,best-effort。

paused 守卫:构造时注入 `is_paused: Callable[[], bool]`(读 AppService
实时状态,不持副本)— paused 期间 client 已 stop,6 op 返 0 + log warning。
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
        # P2 #31:指向"当前 op 的局部取消 Event";无 op 进行时为 None。
        # 赋值发生在每个 op 入口(创建局部 Event 后),置 None 在 op 的
        # finally — 单线程 loop 下两步之间不会被其它 op 穿插写。
        self._cancel_event: asyncio.Event | None = None
        self._batch_started_at: float = 0.0

    # ---------- 取消 ----------

    def cancel_current_batch(self) -> None:
        """取消当前批量操作 — set 当前 op 的局部 Event,op 下一个 item 检查后 break。

        多次调用安全(只是 set 已 set 的 Event);无批量进行时是 no-op
        (不再像旧实现那样 set 一个"下一个 op 会 clear"的共享 Event)。
        """
        if self._cancel_event is not None:
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

    def _begin_op(self) -> asyncio.Event:
        """op 入口:创建本 op 专属的取消 Event 并注册为"当前 op"。

        P2 #31:每个 op 一个新 Event,取代旧的共享 Event + 开头 clear()。
        """
        cancel_event = asyncio.Event()
        self._cancel_event = cancel_event
        self._start_batch_timer()
        return cancel_event

    def _end_op(self, cancel_event: asyncio.Event) -> None:
        """op 退出(含异常):**只在本 op 仍是"当前 op"时**清引用。

        2026-11-26 2.1.7(修 P2 #31 引入的回归):旧实现无条件置 None。
        两个 op 并发时,先注册的那个先结束 → 把后注册、仍在跑的 op 的
        取消信号一并清掉,此时点取消变 no-op。带上自己的 Event 做身份
        比较,后注册的 op 结束前引用一直在,`cancel_current_batch()` 能
        落到它头上。
        """
        if self._cancel_event is cancel_event:
            self._cancel_event = None

    # ---------- 6 批量 op ----------

    async def delete_messages_batch(self, items: list[tuple[int, int]]) -> int:
        """批量删 N 条消息 — 转 MonitorService.delete_messages(带 cancel_event)。

        返成功条数;每条成功 publish MessageDeleted → LIVE view remove_row。
        monitor 内部循环不逐条 emit,进度按 (deleted, total) 末尾发一次。

        2026-11-25 P2 #32:monitor.delete_messages 返回
        `(succeeded, failures)`,部分失败时 failures 逐条组装进
        BatchDone(旧实现部分失败 failures=[] —「查看失败详情」拿空列表)。
        """
        if not items:
            return 0
        if self._is_paused():
            log.warning("BatchService.delete_messages_batch: paused, skip %d items", len(items))
            return 0
        assert self._monitor is not None
        cancel_event = self._begin_op()
        try:
            await self._publish_batch_progress("delete", 0, len(items))
            failures: list[tuple[int, int, str]] = []
            try:
                deleted, op_failures = await self._monitor.delete_messages(
                    items, cancel_event=cancel_event
                )
                failures.extend(op_failures)
            except Exception as exc:
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
                    error="cancelled" if cancel_event.is_set() else None,
                    failures=failures,
                )
            )
            return deleted
        finally:
            self._end_op(cancel_event)

    async def forward_messages(self, items: list[tuple[int, int]], to_chat_id: int) -> int:
        """批量转发 — 按 from_cid group,各走 `_chunks(mids, 100)`(API 上限)。

        已发的 chunk 不撤回(超出控制范围,best-effort)。
        """
        if not items:
            return 0
        if self._is_paused():
            log.warning("BatchService.forward_messages: paused, skip %d items", len(items))
            return 0
        cancel_event = self._begin_op()
        try:
            by_cid: dict[int, list[int]] = {}
            for cid, mid in items:
                by_cid.setdefault(cid, []).append(mid)
            success = 0
            cancelled = False
            failures: list[tuple[int, int, str]] = []
            for from_cid, mids in by_cid.items():
                for chunk in self._chunks(mids, 100):
                    if cancel_event.is_set():
                        cancelled = True
                        break
                    try:
                        await self._client.forward_messages(from_cid, to_chat_id, chunk)
                        success += len(chunk)
                        await self._publish_batch_progress("forward", success, len(items))
                    except Exception as exc:
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
        finally:
            self._end_op(cancel_event)

    async def pin_messages(self, items: list[tuple[int, int]]) -> int:
        """批量钉选 — 按 cid group(per-msg RPC,异常隔离粒度到单条)。"""
        if not items:
            return 0
        if self._is_paused():
            log.warning("BatchService.pin_messages: paused, skip %d items", len(items))
            return 0
        cancel_event = self._begin_op()
        try:
            by_cid: dict[int, list[int]] = {}
            for cid, mid in items:
                by_cid.setdefault(cid, []).append(mid)
            success = 0
            cancelled = False
            failures: list[tuple[int, int, str]] = []
            for cid, msg_ids in by_cid.items():
                if cancel_event.is_set():
                    cancelled = True
                    break
                try:
                    await self._client.pin_messages(cid, msg_ids)
                    success += len(msg_ids)
                    await self._publish_batch_progress("pin", success, len(items))
                except Exception as exc:
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
        finally:
            self._end_op(cancel_event)

    async def unpin_messages(self, items: list[tuple[int, int]]) -> int:
        """批量取消钉选 — 同 pin_messages 模式。"""
        if not items:
            return 0
        if self._is_paused():
            log.warning("BatchService.unpin_messages: paused, skip %d items", len(items))
            return 0
        cancel_event = self._begin_op()
        try:
            by_cid: dict[int, list[int]] = {}
            for cid, mid in items:
                by_cid.setdefault(cid, []).append(mid)
            success = 0
            cancelled = False
            failures: list[tuple[int, int, str]] = []
            for cid, msg_ids in by_cid.items():
                if cancel_event.is_set():
                    cancelled = True
                    break
                try:
                    await self._client.unpin_messages(cid, msg_ids)
                    success += len(msg_ids)
                    await self._publish_batch_progress("unpin", success, len(items))
                except Exception as exc:
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
        finally:
            self._end_op(cancel_event)

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
        cancel_event = self._begin_op()
        try:
            success = 0
            cancelled = False
            failures: list[tuple[int, int, str]] = []
            for cid, mid in items:
                if cancel_event.is_set():
                    cancelled = True
                    break
                try:
                    await self._client.add_reaction(cid, mid, emoji, is_big=is_big)
                    success += 1
                    await self._publish_batch_progress("react", success, len(items))
                except Exception as exc:
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
        finally:
            self._end_op(cancel_event)

    async def remove_reaction(self, items: list[tuple[int, int]], emoji: str) -> int:
        """批量取消 emoji 回应 — 同 add_reaction 模式。"""
        if not items or not emoji:
            return 0
        if self._is_paused():
            log.warning("BatchService.remove_reaction: paused, skip %d items", len(items))
            return 0
        cancel_event = self._begin_op()
        try:
            success = 0
            cancelled = False
            failures: list[tuple[int, int, str]] = []
            for cid, mid in items:
                if cancel_event.is_set():
                    cancelled = True
                    break
                try:
                    await self._client.remove_reaction(cid, mid, emoji)
                    success += 1
                    await self._publish_batch_progress("unreact", success, len(items))
                except Exception as exc:
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
        finally:
            self._end_op(cancel_event)
