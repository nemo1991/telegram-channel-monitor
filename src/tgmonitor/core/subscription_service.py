"""SubscriptionService — 频道订阅 / 退订 / 列表域 façade(从 `AppService` 拆出)。

2026-08-29 v1.5.0 PR #A2:把 AppService 中「频道订阅 + 消息查询」抽到这里,
AppService 退化为组合根 + 转发。包含:
  - `list_joined_channels`(best-effort UX,不走 storage)
  - `list_subscribed_channels`(已订真理,走 storage)
  - `subscribe_channel` / `unsubscribe_channel`(upsert + set_subscribed + 发事件)
  - `list_messages`(查 storage;channel_ids=None 时拉已订真理)
  - `subscribe_updates`(实时流)— 仅转给 TelegramClient.subscribe_updates

设计:
  - 持 `bus + client + storage`;鉴权 / 媒体 / 导出归其它 service
  - 公共方法签名 1:1 转发,UI 现有调用面不动
"""

from __future__ import annotations

import logging
from datetime import datetime

from tgmonitor.core.dto import ChannelDTO, MessageDTO
from tgmonitor.core.events import (
    ChannelSubscribed,
    ChannelUnsubscribed,
    EventBus,
)
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream

log = logging.getLogger(__name__)


class SubscriptionService:
    """频道订阅 / 退订 / 列表 / 消息查询。"""

    def __init__(
        self,
        bus: EventBus,
        client: TelegramClient,
        storage: StorageRepository,
    ) -> None:
        """持 3 个依赖:bus + client + storage(无 objects / monitor / settings)。"""
        self._bus = bus
        self._client = client
        self._storage = storage

    # ---------- 频道列表 ----------

    async def list_joined_channels(self) -> list[ChannelDTO]:
        """已加入 Telegram 频道(best-effort UX,不走 storage)。"""
        return await self._client.list_joined_channels()

    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        """已订阅频道 — **单一真理**走 storage(删 `_subscribed` cache 后)。

        # 2026-07-31 删 `self._subscribed` cache 后这是 AppService 唯一
        # 「订阅列表」读取入口,被 VM / monitor / channel_widget 复用。
        """
        return await self._storage.list_subscribed_channels()

    # ---------- 订阅 / 退订 ----------

    async def subscribe_channel(self, channel: ChannelDTO) -> None:
        """订阅一个频道 — upsert 完整元数据 + 设 subscribed=True + 发事件。

        # 先 upsert 完整信息(标题等),再设 subscribed=True —
        # 后者用 set_channel_subscribed 不会改其他字段。
        """
        await self._storage.upsert_channel(channel)
        await self._storage.set_channel_subscribed(channel.id, True)
        await self._bus.publish(ChannelSubscribed(channel=channel))

    async def unsubscribe_channel(self, channel_id: int) -> None:
        """退订 — 关订阅标志但保留历史 + 元数据。

        # 退订 = 关闭订阅标志,不动元数据 / 消息。
        # 历史消息继续在 storage 里 — 用户重新订阅能看到老历史。
        # 元数据继续被 sync 刷新 — 退订后仍能反映 title/username 变化。
        #
        # 2026-07-31 修 SUBSCRIBED_DRIFT_ANALYSIS #A:之前 storage 失败被
        # `log.exception` 吞后仍 emit `ChannelUnsubscribed`,UI 移走视觉
        # 元素,但 storage 持久化记录仍 `is_subscribed=True`,下次启动 reload
        # → 该频道被"恢复订阅",用户视角看不出退订成功未。现在让 storage
        # 异常直接 raise(不静默吞),让 VM / ChannelWidget 的 `run_coro` 走
        # 统一异常路径 → UI 看到 ErrorOccurred 而非假成功。
        """
        await self._storage.set_channel_subscribed(channel_id, False)
        await self._bus.publish(ChannelUnsubscribed(channel_id=channel_id))

    # ---------- 消息查询(供 UI 显示) ----------

    async def list_messages(
        self,
        channel_ids: list[int] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int | None = 200,
    ) -> list[MessageDTO]:
        """查消息 — `channel_ids=None` 时走 storage「已订」真理(修 #B 双真理问题)。

        # `channel_ids=None` 时从 storage 取**当前真理**(已订频道列表),
        # 不用 in-memory cache — 跟 `list_subscribed_channels()` 是同一个真理。
        # 2026-07-31 修 SUBSCRIBED_DRIFT_ANALYSIS #B。
        """
        if channel_ids is None:
            channel_ids = [c.id for c in await self._storage.list_subscribed_channels()]
        if not channel_ids:
            return []
        return await self._storage.list_messages(
            channel_ids,
            date_from,
            date_to,
            limit,
        )

    # ---------- 实时流 ----------

    def subscribe_updates(self) -> UpdateStream:
        """订阅实时更新流(转给 UI;关 app 时 stop_monitor 统一 aclose)。

        返回的 stream 由调用方持有并 aclose;`AppService.stop_monitor`
        会维护一个 list 统一关闭 — 此方法只是把 client.subscribe_updates
        暴露给 UI。
        """
        return self._client.subscribe_updates()
