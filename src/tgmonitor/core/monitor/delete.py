"""删除逻辑组件 — `MonitorService` 的 `delete_message` / `delete_messages`
+ `_delete_with_orphan_check` + `_handle_message_deleted`。

2026-11-11 v2.0.0 Phase 3-C3:从 `DeleteMixin` 改为显式组合组件。
2026-11-25 P2-M4:组件改为持有共享 `MonitorRuntime`,不再反向持有
`MonitorService`;依赖经 `self._rt.*` 访问,跨组件 seam 经
`self._rt.delete_with_orphan_check` 晚绑定回调(测试在 monitor 实例上
patch 该方法仍被 honoring)。
"""

from __future__ import annotations

import asyncio
import logging

from tgmonitor.core.events import ErrorOccurred, MessageDeleted
from tgmonitor.core.monitor._runtime import MonitorRuntime

log = logging.getLogger(__name__)


class DeleteManager:
    """删除消息 + 孤儿 bytes 清理组件。集中 4 个 method:

    - `delete_message` — 删单条 + publish MessageDeleted
    - `delete_messages` — 批量删(支持 cancel_event)
    - `_delete_with_orphan_check` — 核心:row + refcount + 孤儿 bytes
    - `_handle_message_deleted` — bus 订阅:`updateDeleteMessages`

    经 `self._rt` 访问共享依赖:`bus / storage / objects`,
    经 `rt.delete_with_orphan_check` 回调宿主 seam。
    """

    def __init__(self, rt: MonitorRuntime) -> None:
        self._rt = rt

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """删单条消息 + 清孤儿 bytes + 发 MessageDeleted 事件。

        2026-08-24:顺手清 bytes — 对该 message 的每个 `media.object_key`
        做一次 refcount 检查(`_count_media_with_object_key`),refcount=0 时
        才 `objects.delete(key)`,避免误删跨消息去重场景下其它 message 还在
        用的 bytes。清理失败只 log,不阻断删除。

        2026-08-27 PR #11:走 `_delete_with_orphan_check` 复用;publish 仍在
        这里(AppService 用户触发的删除需要通知 UI)。
        """
        cb = self._rt.delete_with_orphan_check
        assert cb is not None
        await cb(channel_id, telegram_msg_id)
        await self._rt.bus.publish(
            MessageDeleted(channel_id=channel_id, telegram_msg_id=telegram_msg_id)
        )

    async def delete_messages(
        self,
        items: list[tuple[int, int]],
        cancel_event: asyncio.Event | None = None,
    ) -> tuple[int, list[tuple[int, int, str]]]:
        """2026-09-08 v1.7.0:批量删 N 条消息 + 清孤儿 + 发 N 次 MessageDeleted。

        按 (channel_id,) 分组后各调 `storage.delete_messages` 一次性 SQL,
        复用 `_delete_with_orphan_check` 的 refcount 语义(串行,保证共享
        object_key 的两条消息被删时 refcount 正确递减)。单条失败不阻断。

        2026-09-11 v1.7.4 (regression fix):`cancel_event` 可选 — 每条 RPC
        前查 `cancel_event.is_set()`,True 则 break(不 publish,不发
        MessageDeleted);原 facade 一次跑完整批无法中断,改为可选事件以
        兼容旧调用点 + 让 AppService.delete_messages_batch 走真批量路径
        而非退化为 per-item。

        2026-11-25 P2 #32:返回值从裸 int 改为 `(succeeded, failures)` —
        失败条目 `(cid, mid, reason)` 逐条收集,让 BatchService 能把部分
        失败明细组装进 BatchDone.failures(旧实现吞异常只 log,「查看失败
        详情」拿空列表)。

        Out of scope(v1.8+):整体 refcount 收集优化(N×media → 1 round-trip)。
        """
        if not items:
            return 0, []
        succeeded: list[tuple[int, int]] = []
        failures: list[tuple[int, int, str]] = []
        cb = self._rt.delete_with_orphan_check
        assert cb is not None
        for cid, mid in items:
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                await cb(cid, mid)
                succeeded.append((cid, mid))
            except Exception as exc:
                log.exception("delete_messages(%s, %s) failed", cid, mid)
                failures.append((cid, mid, str(exc) or exc.__class__.__name__))
        # 只 publish 成功的(让 UI remove_row;失败的保持原样,用户可重试)
        for cid, mid in succeeded:
            await self._rt.bus.publish(MessageDeleted(channel_id=cid, telegram_msg_id=mid))
        return len(succeeded), failures

    async def _handle_message_deleted(self, event: MessageDeleted) -> None:
        """2026-08-27 v1.4.0 PR #11:Telegram 推送消息删除 → 落库删 row +
        清孤儿 bytes。**不**再 publish(避免与发布者形成无限循环)。
        """
        try:
            cb = self._rt.delete_with_orphan_check
            assert cb is not None
            await cb(event.channel_id, event.telegram_msg_id)
        except Exception as e:
            log.exception("handle MessageDeleted failed: %s", e)
            await self._rt.bus.publish(
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
        rt = self._rt
        old = await rt.storage.get_message(channel_id, telegram_msg_id)
        await rt.storage.delete_message(channel_id, telegram_msg_id)
        if not old:
            return
        for med in old.media:
            key = med.object_key
            if not key:
                continue
            try:
                # 2026-08-25 PR #3:refcount 下沉到 storage 后端
                # (SQL count / InMemory 顺序扫)。
                n = await rt.storage.count_media_by_object_key(key)
            except Exception:
                log.exception("count media by key failed: %s", key)
                continue
            if n == 0 and rt.objects is not None:
                try:
                    await rt.objects.delete(key)
                    log.info(
                        "monitor delete orphan bytes: channel=%s msg=%s key=%s",
                        channel_id,
                        telegram_msg_id,
                        key,
                    )
                except Exception:
                    log.warning(
                        "monitor delete bytes %s failed (already gone?)",
                        key,
                        exc_info=True,
                    )
