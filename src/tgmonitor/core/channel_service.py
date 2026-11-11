"""ChannelService — 频道订阅域服务。

2026-11-11 v2.0.0 Phase 3:从 `SubscriptionService` 拆出频道部分
(消息查询归 `MessageService`),白名单转发从 AppService 移入。

职责:
  - `list_joined_channels`(best-effort UX,不走 storage)
  - `list_subscribed_channels`(已订真理,走 storage)
  - `subscribe_channel` / `unsubscribe_channel`(upsert + set_subscribed + 发事件)
  - `add_to_whitelist` / `remove_from_whitelist`(sync,转 MonitorService)

设计:持 `bus + client + storage + monitor`;monitor 可选(纯测试 /
未启动路径为 None,白名单操作 assert 锁死契约)。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import (
    ChannelSubscribed,
    ChannelUnsubscribed,
    EventBus,
)
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient

if TYPE_CHECKING:
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)


class ChannelService:
    """频道订阅 / 退订 / 列表 + 监听白名单同步。"""

    def __init__(
        self,
        bus: EventBus,
        client: TelegramClient,
        storage: StorageRepository,
        monitor: MonitorService | None = None,
    ) -> None:
        """持 4 个依赖:bus + client + storage + monitor(白名单热路径)。"""
        self._bus = bus
        self._client = client
        self._storage = storage
        self._monitor = monitor

    # ---------- 频道列表 ----------

    async def list_joined_channels(self) -> list[ChannelDTO]:
        """已加入 Telegram 频道(best-effort UX,不走 storage)。"""
        return await self._client.list_joined_channels()

    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        """已订阅频道 — **单一真理**走 storage(删 `_subscribed` cache 后)。"""
        return await self._storage.list_subscribed_channels()

    # ---------- 订阅 / 退订 ----------

    async def subscribe_channel(self, channel: ChannelDTO) -> None:
        """订阅一个频道 — upsert 完整元数据 + 设 subscribed=True + 发事件。

        先 upsert 完整信息(标题等),再设 subscribed=True —
        后者用 set_channel_subscribed 不会改其他字段。
        """
        await self._storage.upsert_channel(channel)
        await self._storage.set_channel_subscribed(channel.id, True)
        await self._bus.publish(ChannelSubscribed(channel=channel))

    async def unsubscribe_channel(self, channel_id: int) -> None:
        """退订 — 关订阅标志但保留历史 + 元数据。

        退订 = 关闭订阅标志,不动元数据 / 消息。历史消息继续在 storage 里,
        用户重新订阅能看到老历史;元数据继续被 sync 刷新。

        storage 异常直接 raise(不静默吞)— 让 UI 的 `run_coro` 走统一
        异常路径显示 ErrorOccurred,而非「视觉退订成功、持久化仍 subscribed」
        的假成功(SUBSCRIBED_DRIFT_ANALYSIS #A)。
        """
        await self._storage.set_channel_subscribed(channel_id, False)
        await self._bus.publish(ChannelUnsubscribed(channel_id=channel_id))

    # ---------- 监听白名单(sync,转 MonitorService)----------

    def add_to_whitelist(self, channel_id: int) -> None:
        """订阅后立即把频道加入监听白名单 — sync set 操作,无 await。

        白名单操作在 bootstrap 完成后调,monitor 必非 None;
        assert 锁死契约,启动期退化路径显式报错。
        """
        assert self._monitor is not None
        self._monitor.add_to_whitelist(channel_id)

    def remove_from_whitelist(self, channel_id: int) -> None:
        """退订后从监听白名单摘掉频道 — sync,幂等。"""
        assert self._monitor is not None
        self._monitor.remove_from_whitelist(channel_id)
