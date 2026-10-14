"""AppService 批量操作 — 2026-10-12 plan-batch3 目标 2 step 2.3。

从 `AppService` 抽提 7 个批量 op + cancel + 3 ETA helper + _chunks:

7 batch op(2026-09-08~14 v1.7.0~5,UI 批量多选触发):
- `delete_messages_batch` — 走 `monitor.delete_messages(items, cancel_event)`
- `mark_messages_read` — 走 `client.mark_messages_read(cid, msg_ids)`
- `forward_messages` — 走 `client.forward_messages` + `_chunks(mids, 100)`
- `pin_messages` / `unpin_messages` — 走 `client.pin_messages` / `unpin_messages`
- `add_reaction` / `remove_reaction` — 走 `client.add_reaction` / `remove_reaction`

user-facing 取消入口(2026-09-10 v1.7.3):`cancel_current_batch(app)` —
UI BatchProgressDialog「取消」按钮调用,设 `_cancel_event` 让 7 batch op
内部循环 break + 发 BatchDone(error='cancelled')。

3 ETA helper(2026-09-11 v1.7.4):
- `_start_batch_timer(app)` — 7 op 开头记 `time.monotonic()` 起点
- `_compute_rate(app, processed)` — 算 elapsed / rate_per_second
- `_publish_batch_progress(app, op, processed, total)` — 5 处 emit 集中

设计原则(同 `_backend_sync` / `_media_actions`):
- plain module-level function(非 class)
- 第一参数 `app: AppService`(走 `app._is_paused` / `app._cancel_event` /
  `app._batch_started_at` / `app.monitor` / `app.client` / `app.bus` 共享 mutable state)
- 公共方法签名 1:1 保留 — UI 现有调用面零变化
- 3 helper 不再 facade 暴露(原本就内部 helper,被 7 op 在 module 内部直接调)

mutable state 共享契约(同 plan-batch2 monitor/service.py 拆 facade):
所有 7 op + 3 helper 直接读写 `app._is_paused` / `app._cancel_event` /
`app._batch_started_at` — 不在 module 内持副本,避免 stale ref。文档
明示「不要在 module 内 local cache `app._is_paused`」。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from tgmonitor.core.events import BatchDone, BatchProgress

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService

log = logging.getLogger(__name__)


# ---------- module-level helper:分片(forwardMessages 上限 100)----------


def _chunks(items: list[int], size: int) -> list[list[int]]:
    """2026-09-09 v1.7.2:把 list 切成 ≤size 的连续子列表(forwardMessages 上限 100)。

    保序、均匀(`size` 不变,最后一片可能 < size)。
    空 list 返 `[]`(不返 `[[]]`)。
    """
    if not items:
        return []
    return [items[i : i + size] for i in range(0, len(items), size)]


# ---------- 3 ETA helper(2026-09-11 v1.7.4)----------


def _start_batch_timer(app: AppService) -> None:
    """2026-09-11 v1.7.4:批量开始 — 记 `time.monotonic()` 起点。

    每个 7 op 开头调一次。简单到不需要 lock — Python `time.monotonic()`
    跨 asyncio task 共享,UI 进度计算只关心 elapsed,精度 1ms 足矣。
    """
    app._batch_started_at = time.monotonic()


def _compute_rate(app: AppService, processed: int) -> tuple[float, float]:
    """2026-09-11 v1.7.4:算 elapsed_seconds / rate_per_second — 给 BatchProgress 用。

    Returns:
        `(elapsed_seconds, rate_per_second)` — 后者 = processed / elapsed。
        elapsed < 1e-3 时 rate 返 0.0(避免除零;UI 显示时也跳过 ETA)。
    """
    elapsed = time.monotonic() - app._batch_started_at
    rate = processed / elapsed if elapsed > 1e-3 else 0.0
    return elapsed, rate


async def _publish_batch_progress(app: AppService, op: str, processed: int, total: int) -> None:
    """2026-09-11 v1.7.4:发布 BatchProgress,自动加 elapsed_seconds / rate_per_second。

    facade 改 helper 后 5 处循环内的 BatchProgress(...) 调改为
    `await self._publish_batch_progress(op, success, total)` — 替换
    4 个 BatchProgress emit 实例 + 1 个 delete 开始 emit。
    """
    elapsed, rate = _compute_rate(app, processed)
    await app.bus.publish(
        BatchProgress(
            op=op,
            processed=processed,
            total=total,
            elapsed_seconds=elapsed,
            rate_per_second=rate,
        )
    )


# ---------- user-facing 取消入口 ----------


def cancel_current_batch(app: AppService) -> None:
    """2026-09-10 v1.7.3:取消当前批量操作 — set Event,facade 下一个 item 检查后 break。

    不调 task.cancel:批量 facade 在主 task loop 里 await,设 Event 即可让循环跳出;
    task.cancel 会触发 CancelledError 被 run_coro 吞,无法发
    BatchDone(error='cancelled')。已发 TDLib RPC 可能 server-side 仍完成 —
    best-effort,UI 上 BatchProgressDialog 提示「操作中断」即可。

    多次调用安全(只是 set 已 set 的 Event);无批量进行时也无副作用
    (下一次 facade 开头 _cancel_event.clear())。
    """
    app._cancel_event.set()


# ---------- 7 批量 op ----------


async def delete_messages_batch(app: AppService, items: list[tuple[int, int]]) -> int:
    """2026-09-08 v1.7.0:批量删 N 条消息 — 转发 MonitorService.delete_messages。

    返成功条数(失败的保持原样 + log,UI 可重试)。每条成功都 publish
    MessageDeleted 事件 → LIVE view remove_row 自动刷新。

    2026-09-09 v1.7.2:加 BatchProgress / BatchDone 进度事件 —
    MainWindow 据此驱动 BatchProgressDialog 进度条 + 收尾。

    2026-09-10 v1.7.3:支持 cancel_current_batch — facade 内每次 RPC 前查
    _cancel_event.is_set(),True 则 break + 发 BatchDone(error='cancelled')。
    delete_messages 走 monitor 同步方法,实际取消点是 RPC 之前;
    已发出的 RPC 仍 server-side 完成,best-effort。

    2026-09-11 v1.7.4 (regression fix):原 facade 一次性调
    `monitor.delete_messages(items)`(整批同步跑完),无法在循环中途
    中断。给 monitor.delete_messages 加 cancel_event 可选参数,
    facade 透传 — 保留批量 SQL 路径 + 支持中途 break。补 _is_paused
    守卫与其他 6 facade 一致。
    """
    if not items:
        return 0
    if app._is_paused:
        log.warning(
            "AppService.delete_messages_batch: monitor paused, skip %d items",
            len(items),
        )
        return 0
    assert app.monitor is not None
    app._cancel_event.clear()
    _start_batch_timer(app)
    await _publish_batch_progress(app, "delete", 0, len(items))
    # 2026-09-14 v1.7.5 PR #6 (P0-J):失败明细 — monitor.delete_messages 走
    # 同步 SQL,per-item 异常不抛 → 整批 try/except 兜底,记 failures。
    failures: list[tuple[int, int, str]] = []
    try:
        deleted = await app.monitor.delete_messages(items, cancel_event=app._cancel_event)
    except Exception as exc:  # noqa: BLE001
        log.exception("delete_messages_batch 全批失败")
        deleted = 0
        for cid, mid in items:
            failures.append((cid, mid, str(exc) or exc.__class__.__name__))
    # ETA 进度:由于 monitor 内部循环不会逐条 emit,我们只能按 (deleted, total)
    # 在末尾发一次完整进度。UI dialog 看的是「已完成 N」,ETA 依赖 elapsed,
    # 也能从 BatchDone 前的最后一次 emit 算。
    await _publish_batch_progress(app, "delete", deleted, len(items))
    await app.bus.publish(
        BatchDone(
            op="delete",
            succeeded=deleted,
            failed=len(items) - deleted,
            error="cancelled" if app._cancel_event.is_set() else None,
            failures=failures,
        )
    )
    return deleted


async def mark_messages_read(app: AppService, items: list[tuple[int, int]]) -> int:
    """2026-09-08 v1.7.0:批量标已读 — 走 TG client.viewMessages。

    按 channel_id 分组,各调一次 `client.mark_messages_read(cid, msg_ids)`。
    paused 状态(client 已 stop)返 0 + log warning,不抛 UI 错误。

    2026-09-10 v1.7.3:支持 cancel_current_batch — 每个 cid group 前查
    _cancel_event.is_set(),True 则 break + 发 BatchDone(error='cancelled')。
    """
    if not items:
        return 0
    if app._is_paused:
        log.warning("AppService.mark_messages_read: client paused, skip %d items", len(items))
        return 0
    app._cancel_event.clear()
    _start_batch_timer(app)
    # 按 cid 分组
    by_cid: dict[int, list[int]] = {}
    for cid, mid in items:
        by_cid.setdefault(cid, []).append(mid)
    success = 0
    cancelled = False
    failures: list[tuple[int, int, str]] = []
    for cid, msg_ids in by_cid.items():
        if app._cancel_event.is_set():
            cancelled = True
            break
        try:
            await app.client.mark_messages_read(cid, msg_ids)
            success += len(msg_ids)
            await _publish_batch_progress(app, "mark_read", success, len(items))
        except Exception as exc:  # noqa: BLE001
            log.exception("mark_messages_read(cid=%s) failed", cid)
            for mid in msg_ids:
                failures.append((cid, mid, str(exc) or exc.__class__.__name__))
    await app.bus.publish(
        BatchDone(
            op="mark_read",
            succeeded=success,
            failed=len(items) - success,
            error="cancelled" if cancelled else None,
            failures=failures,
        )
    )
    return success


async def forward_messages(app: AppService, items: list[tuple[int, int]], to_chat_id: int) -> int:
    """2026-09-09 v1.7.2:批量转发 — 走 TG client.forwardMessages。

    `items` 是 `(from_cid, mid)` 对;按 from_cid group,各走
    `_chunks(mids, 100)`(TDLib `forwardMessages` 上限)。paused 返 0。

    2026-09-10 v1.7.3:支持 cancel_current_batch — 每个 chunk 前查
    _cancel_event.is_set(),True 则 break + 发 BatchDone(error='cancelled')。
    已发的 chunk 不撤回(超出 facade 控制范围,best-effort)。
    """
    if not items:
        return 0
    if app._is_paused:
        log.warning("AppService.forward_messages: client paused, skip %d items", len(items))
        return 0
    app._cancel_event.clear()
    _start_batch_timer(app)
    by_cid: dict[int, list[int]] = {}
    for cid, mid in items:
        by_cid.setdefault(cid, []).append(mid)
    success = 0
    cancelled = False
    failures: list[tuple[int, int, str]] = []
    for from_cid, mids in by_cid.items():
        for chunk in _chunks(mids, 100):
            if app._cancel_event.is_set():
                cancelled = True
                break
            try:
                await app.client.forward_messages(from_cid, to_chat_id, chunk)
                success += len(chunk)
                await _publish_batch_progress(app, "forward", success, len(items))
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "forward_messages(%s → %s, %d) failed", from_cid, to_chat_id, len(chunk)
                )
                for mid in chunk:
                    failures.append((from_cid, mid, str(exc) or exc.__class__.__name__))
        if cancelled:
            break
    await app.bus.publish(
        BatchDone(
            op="forward",
            succeeded=success,
            failed=len(items) - success,
            error="cancelled" if cancelled else None,
            failures=failures,
        )
    )
    return success


async def pin_messages(app: AppService, items: list[tuple[int, int]]) -> int:
    """2026-09-09 v1.7.2:批量钉选 — 走 TG client.pinMessages(per-cid,per-msg)。

    TDLib `pinChatMessage` 一次一条;按 cid group 减少 RPC 调用次数。

    2026-09-10 v1.7.3:支持 cancel_current_batch — 每个 cid group 前查
    _cancel_event.is_set(),True 则 break + 发 BatchDone(error='cancelled')。
    """
    if not items:
        return 0
    if app._is_paused:
        log.warning("AppService.pin_messages: client paused, skip %d items", len(items))
        return 0
    app._cancel_event.clear()
    _start_batch_timer(app)
    by_cid: dict[int, list[int]] = {}
    for cid, mid in items:
        by_cid.setdefault(cid, []).append(mid)
    success = 0
    cancelled = False
    failures: list[tuple[int, int, str]] = []
    for cid, msg_ids in by_cid.items():
        if app._cancel_event.is_set():
            cancelled = True
            break
        try:
            await app.client.pin_messages(cid, msg_ids)
            success += len(msg_ids)
            await _publish_batch_progress(app, "pin", success, len(items))
        except Exception as exc:  # noqa: BLE001
            log.exception("pin_messages(cid=%s) failed", cid)
            for mid in msg_ids:
                failures.append((cid, mid, str(exc) or exc.__class__.__name__))
    await app.bus.publish(
        BatchDone(
            op="pin",
            succeeded=success,
            failed=len(items) - success,
            error="cancelled" if cancelled else None,
            failures=failures,
        )
    )
    return success


async def unpin_messages(app: AppService, items: list[tuple[int, int]]) -> int:
    """2026-09-09 v1.7.2:批量取消钉选 — 走 TG client.unpinMessages。

    2026-09-10 v1.7.3:支持 cancel_current_batch(同 pin_messages 模式)。
    """
    if not items:
        return 0
    if app._is_paused:
        log.warning("AppService.unpin_messages: client paused, skip %d items", len(items))
        return 0
    app._cancel_event.clear()
    _start_batch_timer(app)
    by_cid: dict[int, list[int]] = {}
    for cid, mid in items:
        by_cid.setdefault(cid, []).append(mid)
    success = 0
    cancelled = False
    failures: list[tuple[int, int, str]] = []
    for cid, msg_ids in by_cid.items():
        if app._cancel_event.is_set():
            cancelled = True
            break
        try:
            await app.client.unpin_messages(cid, msg_ids)
            success += len(msg_ids)
            await _publish_batch_progress(app, "unpin", success, len(items))
        except Exception as exc:  # noqa: BLE001
            log.exception("unpin_messages(cid=%s) failed", cid)
            for mid in msg_ids:
                failures.append((cid, mid, str(exc) or exc.__class__.__name__))
    await app.bus.publish(
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
    app: AppService,
    items: list[tuple[int, int]],
    emoji: str,
    *,
    is_big: bool = False,
) -> int:
    """2026-09-09 v1.7.2:批量 emoji 回应 — 走 TG client.add_reaction(per-msg)。

    TDLib `addMessageReaction` 一次一条;按 cid group 仅做日志可读性,
    实际调用仍是 per-msg(异常隔离粒度到单条)。

    2026-09-10 v1.7.3:支持 cancel_current_batch — 每条 RPC 前查
    _cancel_event.is_set(),True 则 break + 发 BatchDone(error='cancelled')。
    2026-09-14 v1.7.5 PR #6 (P0-J):失败明细 — 每条失败 append (cid, mid, error)。
    """
    if not items or not emoji:
        return 0
    if app._is_paused:
        log.warning("AppService.add_reaction: client paused, skip %d items", len(items))
        return 0
    app._cancel_event.clear()
    _start_batch_timer(app)
    success = 0
    cancelled = False
    failures: list[tuple[int, int, str]] = []
    for cid, mid in items:
        if app._cancel_event.is_set():
            cancelled = True
            break
        try:
            await app.client.add_reaction(cid, mid, emoji, is_big=is_big)
            success += 1
            await _publish_batch_progress(app, "react", success, len(items))
        except Exception as exc:  # noqa: BLE001
            log.exception("add_reaction(%s, %s, %s) failed", cid, mid, emoji)
            failures.append((cid, mid, str(exc) or exc.__class__.__name__))
    await app.bus.publish(
        BatchDone(
            op="react",
            succeeded=success,
            failed=len(items) - success,
            error="cancelled" if cancelled else None,
            failures=failures,
        )
    )
    return success


async def remove_reaction(app: AppService, items: list[tuple[int, int]], emoji: str) -> int:
    """2026-09-09 v1.7.2:批量取消 emoji 回应 — 走 TG client.remove_reaction。

    2026-09-10 v1.7.3:支持 cancel_current_batch(同 add_reaction 模式)。
    2026-09-14 v1.7.5 PR #6 (P0-J):失败明细。
    """
    if not items or not emoji:
        return 0
    if app._is_paused:
        log.warning("AppService.remove_reaction: client paused, skip %d items", len(items))
        return 0
    app._cancel_event.clear()
    _start_batch_timer(app)
    success = 0
    cancelled = False
    failures: list[tuple[int, int, str]] = []
    for cid, mid in items:
        if app._cancel_event.is_set():
            cancelled = True
            break
        try:
            await app.client.remove_reaction(cid, mid, emoji)
            success += 1
            await _publish_batch_progress(app, "unreact", success, len(items))
        except Exception as exc:  # noqa: BLE001
            log.exception("remove_reaction(%s, %s, %s) failed", cid, mid, emoji)
            failures.append((cid, mid, str(exc) or exc.__class__.__name__))
    await app.bus.publish(
        BatchDone(
            op="unreact",
            succeeded=success,
            failed=len(items) - success,
            error="cancelled" if cancelled else None,
            failures=failures,
        )
    )
    return success


__all__ = [
    "cancel_current_batch",
    "delete_messages_batch",
    "mark_messages_read",
    "forward_messages",
    "pin_messages",
    "unpin_messages",
    "add_reaction",
    "remove_reaction",
]
