"""ChannelService — 频道订阅域服务。

2026-11-11 v2.0.0 Phase 3:从 `SubscriptionService` 拆出频道部分
(消息查询归 `MessageService`),白名单转发从 AppService 移入。
2026-11-17 v2.0.3:事件正名 ChannelMonitored/Unmonitored;新增
`refresh_joined_channels`(TG 订阅状态落库 + 退订级联停监听 + 空返回保护)。

职责:
  - `list_joined_channels`(best-effort UX,不走 storage)
  - `list_subscribed_channels`(已监听真理,走 storage)
  - `refresh_joined_channels`(v2.0.3:刷新落库 + 级联)
  - `subscribe_channel` / `unsubscribe_channel`(upsert + set_subscribed + 发事件)
  - `add_to_whitelist` / `remove_from_whitelist`(sync,转 MonitorService)

设计:持 `bus + client + storage + monitor`;monitor 可选(纯测试 /
未启动路径为 None,白名单操作 assert 锁死契约)。

术语(v2.0.3 分清两个概念):
  - **监听(monitored)**:本应用是否收这个频道的消息 = 白名单 = 物理列 subscribed。
  - **TG 订阅(tg_subscribed)**:该频道在 Telegram 侧是否仍订阅/加入。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import (
    ChannelMonitored,
    ChannelUnmonitored,
    ErrorOccurred,
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
        """对本应用**开启监听**一个频道 — upsert 元数据 + 设 monitored=True + 发事件。

        先 upsert 完整信息(标题等),再设监听标志 —
        后者用 set_channel_subscribed 不会改其他字段。
        """
        await self._storage.upsert_channel(channel)
        await self._storage.set_channel_subscribed(channel.id, True)
        await self._bus.publish(ChannelMonitored(channel=channel))

    async def unsubscribe_channel(self, channel_id: int) -> None:
        """本应用**取消监听** — 关监听标志但保留历史 + 元数据 + TG 订阅状态。

        取消监听 = 关闭监听标志,不动元数据 / 消息 / tg_subscribed。历史消息继续
        在 storage 里,用户重新监听能看到老历史;TG 侧仍订阅则 tg_subscribed 不变。

        storage 异常直接 raise(不静默吞)— 让 UI 的 `run_coro` 走统一
        异常路径显示 ErrorOccurred,而非「视觉取消成功、持久化仍 monitored」
        的假成功(SUBSCRIBED_DRIFT_ANALYSIS #A)。
        """
        await self._storage.set_channel_subscribed(channel_id, False)
        await self._bus.publish(ChannelUnmonitored(channel_id=channel_id))

    async def refresh_joined_channels(self) -> list[ChannelDTO]:
        """刷新 TG 已加入频道 → 落库 tg_subscribed + 级联停监听(v2.0.3)。

        语义(用户拍板的双字段模型):
          - TG 列表里的频道 → `tg_subscribed=True`(用专门 setter,不碰 monitored);
          - 库里有行但不在 TG 列表 → `tg_subscribed=False` + 若正在监听则
            `monitored=False` + 摘白名单 + 发 `ChannelUnmonitored`,**保留行与历史**;
          - TG 里新出现但库里没行的频道 → `upsert_channel_metadata` 建档(默认不监听)。

        **空返回保护(最高危,RK-09)**:`list_joined_channels` 在未登录 /
        client closing / 未 ready 时返 `[]`。若拿这个空列表去做"不在列表即退订"
        的级联,会一次清空全库订阅 + 停掉所有监听。因此:
          - `client.state != "ready"` → 整轮跳过,返回 `[]`;
          - 拉取结果为空 → 整轮跳过(视为拉取无效/未登录),不做任何级联。
        只有 client ready **且**拉到非空列表才做全量校正。
        """
        if self._client.state != "ready":
            log.info("refresh_joined_channels skipped: not ready (state=%s)", self._client.state)
            await self._bus.publish(
                ErrorOccurred(
                    source="refresh_joined_channels",
                    message="未登录,无法刷新频道",
                )
            )
            return []

        joined = await self._client.list_joined_channels()
        if not joined:
            # 兜底空列表:未登录 / closing / 拉取失败都可能返 [],绝不据此级联清零。
            log.info(
                "refresh_joined_channels skipped: empty joined list (state=%s)", self._client.state
            )
            await self._bus.publish(
                ErrorOccurred(
                    source="refresh_joined_channels",
                    message="未获取到频道列表(未登录或连接不可用),本次不更新订阅状态",
                )
            )
            return []

        joined_ids = {ch.id for ch in joined}
        # 1) TG 列表里的频道:建档/更新元数据 + 写 tg_subscribed=True(不碰 monitored)。
        for ch in joined:
            await self._storage.upsert_channel_metadata(ch)
            await self._storage.set_channel_tg_subscribed(ch.id, True)

        # 2) 库里有行但不在 TG 列表 → TG 退订,级联停监听(保留行与历史)。
        for existing in await self._storage.list_channels():
            if existing.id in joined_ids:
                continue
            await self._storage.set_channel_tg_subscribed(existing.id, False)
            if existing.is_monitored:
                await self._storage.set_channel_subscribed(existing.id, False)
                if self._monitor is not None:
                    self._monitor.remove_from_whitelist(existing.id)
                await self._bus.publish(ChannelUnmonitored(channel_id=existing.id))

        return joined

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
