"""删除逻辑 mixin — `MonitorService` 的 `delete_message` / `delete_messages`
+ `_delete_with_orphan_check` + `_handle_message_deleted`。

2026-10-10 v1.12.x plan-batch2 目标 2 从 `MonitorService` 抽出:
- `delete_message` / `delete_messages` 公开 API,由 `AppService` 触发
- `_delete_with_orphan_check` 核心:删 row + refcount + 删孤儿 bytes
- `_handle_message_deleted` TDLib `updateDeleteMessages` 订阅

设计:同 `WhitelistMixin` — mixin 期望宿主类持有
`self.bus / storage / objects`,`delete_message` 还依赖
`MessageDeleted` event 类型(由 `core.events` 显式 import,避免双向
循环)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from tgmonitor.core.events import ErrorOccurred, MessageDeleted

if TYPE_CHECKING:
    from tgmonitor.core.events import EventBus
    from tgmonitor.core.objectstore.base import ObjectStore
    from tgmonitor.core.storage.repository import StorageRepository

log = logging.getLogger(__name__)


class DeleteMixin:
    """删除消息 + 孤儿 bytes 清理 mixin。集中 4 个 method:

    - `delete_message` — 删单条 + publish MessageDeleted
    - `delete_messages` — 批量删(支持 cancel_event)
    - `_delete_with_orphan_check` — 核心:row + refcount + 孤儿 bytes
    - `_handle_message_deleted` — bus 订阅:`updateDeleteMessages`

    依赖宿主属性:`self.bus / storage / objects`。
    """

    bus: EventBus
    storage: StorageRepository
    objects: ObjectStore

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """删单条消息 + 清孤儿 bytes + 发 MessageDeleted 事件。

        2026-08-24:顺手清 bytes — 对该 message 的每个 `media.object_key`
        做一次 refcount 检查(`_count_media_with_object_key`),refcount=0 时
        才 `objects.delete(key)`,避免误删跨消息去重场景下其它 message 还在
        用的 bytes。清理失败只 log,不阻断删除。

        2026-08-27 PR #11:走 `_delete_with_orphan_check` 复用;publish 仍在
        这里(AppService 用户触发的删除需要通知 UI)。
        """
        await self._delete_with_orphan_check(channel_id, telegram_msg_id)
        await self.bus.publish(
            MessageDeleted(channel_id=channel_id, telegram_msg_id=telegram_msg_id)
        )

    async def delete_messages(
        self,
        items: list[tuple[int, int]],
        cancel_event: asyncio.Event | None = None,
    ) -> int:
        """2026-09-08 v1.7.0:批量删 N 条消息 + 清孤儿 + 发 N 次 MessageDeleted。

        按 (channel_id,) 分组后各调 `storage.delete_messages` 一次性 SQL,
        复用 `_delete_with_orphan_check` 的 refcount 语义(串行,保证共享
        object_key 的两条消息被删时 refcount 正确递减)。单条失败不阻断,
        返成功条数。

        2026-09-11 v1.7.4 (regression fix):`cancel_event` 可选 — 每条 RPC
        前查 `cancel_event.is_set()`,True 则 break(不 publish,不发
        MessageDeleted);原 facade 一次跑完整批无法中断,改为可选事件以
        兼容旧调用点 + 让 AppService.delete_messages_batch 走真批量路径
        而非退化为 per-item。

        Out of scope(v1.8+):整体 refcount 收集优化(N×media → 1 round-trip)。
        """
        if not items:
            return 0
        succeeded: list[tuple[int, int]] = []
        for cid, mid in items:
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                await self._delete_with_orphan_check(cid, mid)
                succeeded.append((cid, mid))
            except Exception:  # noqa: BLE001
                log.exception("delete_messages(%s, %s) failed", cid, mid)
        # 只 publish 成功的(让 UI remove_row;失败的保持原样,用户可重试)
        for cid, mid in succeeded:
            await self.bus.publish(MessageDeleted(channel_id=cid, telegram_msg_id=mid))
        return len(succeeded)

    async def _handle_message_deleted(self, event: MessageDeleted) -> None:
        """2026-08-27 v1.4.0 PR #11:TDLib `updateDeleteMessages` → 落库删 row +
        清孤儿 bytes。**不**再 publish(避免与发布者形成无限循环)。
        """
        try:
            await self._delete_with_orphan_check(event.channel_id, event.telegram_msg_id)
        except Exception as e:  # noqa: BLE001
            log.exception("handle MessageDeleted failed: %s", e)
            await self.bus.publish(
                ErrorOccurred(
                    source="monitor.delete",
                    message=str(e),
                    exception=e,
                )
            )

    async def _delete_with_orphan_check(
        self,
        channel_id: int,
        telegram_msg_id: int,
    ) -> None:
        """PR #11:删 row + 清孤儿 bytes 核心逻辑,与 AppService.delete_message 共享。"""
        old = await self.storage.get_message(channel_id, telegram_msg_id)
        await self.storage.delete_message(channel_id, telegram_msg_id)
        if not old:
            return
        for med in old.media:
            key = med.object_key
            if not key:
                continue
            try:
                # 2026-08-25 PR #3:refcount 下沉到 storage 后端
                # (SQL count / Mongo aggregate / InMemory / Jsonl 顺序扫)。
                n = await self.storage.count_media_by_object_key(key)
            except Exception:  # noqa: BLE001
                log.exception("count media by key failed: %s", key)
                continue
            if n == 0 and self.objects is not None:
                try:
                    await self.objects.delete(key)
                    log.info(
                        "monitor delete orphan bytes: channel=%s msg=%s key=%s",
                        channel_id,
                        telegram_msg_id,
                        key,
                    )
                except Exception:  # noqa: BLE001
                    log.warning(
                        "monitor delete bytes %s failed (already gone?)",
                        key,
                        exc_info=True,
                    )
