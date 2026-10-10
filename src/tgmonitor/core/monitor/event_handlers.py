"""6 个 EventBus 订阅 handler — 频道 / 互动 / pin / 删事件。

2026-10-10 v1.12.x plan-batch2 目标 2 从 `MonitorService` 抽出(~270 LOC)。
抽提原因:handler 互相独立(各管一事件),无共享热路径状态(都是
`await storage.X()` 后 publish ErrorOccurred),放 `service.py` 反而成
信息噪音 —— reader 翻 1,000+ LOC 只想找核心消息处理时,handler 散在
中间挡路。

设计:Mixin + 契约(同 `WhitelistMixin`)。宿主类期望持有
`self.bus / storage / client`,handler 用到就调;事件 payload 由 bus
dispatcher 注入。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tgmonitor.core.events import (
    ChannelMetadataChanged,
    ChannelPhotoChanged,
    ChannelTitleChanged,
    ErrorOccurred,
    MessageInteractionsChanged,
    MessagePinChanged,
)

if TYPE_CHECKING:
    from tgmonitor.core.events import EventBus
    from tgmonitor.core.storage.repository import StorageRepository

log = logging.getLogger(__name__)


class EventHandlerMixin:
    """5 个 EventBus 订阅 handler 集中,按事件分 5 方法:

    - `_handle_interactions_changed` — reactions / views 增量
    - `_handle_pin_changed` — server-side pin / unpin
    - `_handle_channel_metadata` — `updateChannel` / `updateSupergroup`
    - `_handle_channel_title_changed` — `updateChatTitle`
    - `_handle_channel_photo_changed` — `updateChatPhoto`

    `MessageDeleted` handler(原第 6 个)在 `DeleteMixin`(同处
    `core/monitor/delete.py`),与 `delete_message` 公开 API / orphan
    清理紧耦合,不放本文件。

    依赖宿主属性:`self.bus / storage`。
    """

    bus: EventBus
    storage: StorageRepository

    async def _handle_interactions_changed(
        self,
        event: MessageInteractionsChanged,
    ) -> None:
        """2026-08-27 v1.4.0 PR #10:TDLib `updateMessageInteractionInfo`
        → storage `update_message_interactions`(views / reactions 增量)。

        落库后**不再 republish**(避免无限循环:EventBus.subscribe 自己是
        反模式);详情面板直接订阅 `MessageInteractionsChanged` 即可,
        落库前/落库后两条事件同 payload,UI 自取所需时点。
        """
        try:
            await self.storage.update_message_interactions(
                event.channel_id,
                event.telegram_msg_id,
                views=event.views,
                # event.reactions 已是 list[ReactionDTO](TDLib 映射层保证),
                # storage 实现负责 list/dict 双形态容错
                reactions=event.reactions,
            )
            log.debug(
                "interactions updated: channel=%s msg=%s views=%s reactions=%d",
                event.channel_id,
                event.telegram_msg_id,
                event.views,
                len(event.reactions) if event.reactions else 0,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("update interactions failed: %s", e)
            await self.bus.publish(
                ErrorOccurred(
                    source="monitor.interactions",
                    message=str(e),
                    exception=e,
                )
            )

    async def _handle_pin_changed(
        self,
        event: MessagePinChanged,
    ) -> None:
        """2026-09-10 v1.7.3:TDLib `updateMessageIsPinned` → 落库 + republish。

        与 `_handle_interactions_changed` 不同的是:这里**必须** republish
        `MessageEdited` — UI 行的 📌 渲染依赖 in-memory DTO 的 `is_pinned`
        字段,而 in-memory DTO 不会自动从 storage 同步(每次 `replace_message`
        都靠 MessageEdited 触发)。republish 走完整 DTO re-fetch 保证 UI 拿到
        最新值。

        与 reactions 的 "不 republish" 区别:reactions 走 detail panel 单独
        订阅,而 pin 只走 LIVE 行的 📌 图标 — 没有 detail panel 兜底,
        必须 republish。
        """
        from tgmonitor.core.events import MessageEdited

        try:
            await self.storage.update_message_pin(
                event.channel_id,
                event.telegram_msg_id,
                event.is_pinned,
            )
            # 重新从 storage 拉完整 DTO(落库可能改变其它字段,虽然本方法
            # 只动 is_pinned;为防 schema 漂移,re-fetch 是稳妥做法)。
            updated = await self.storage.get_message(
                event.channel_id,
                event.telegram_msg_id,
            )
            if updated is not None:
                await self.bus.publish(MessageEdited(message=updated))
            log.debug(
                "pin updated: channel=%s msg=%s pinned=%s",
                event.channel_id,
                event.telegram_msg_id,
                event.is_pinned,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("update pin failed: %s", e)
            await self.bus.publish(
                ErrorOccurred(
                    source="monitor.pin",
                    message=str(e),
                    exception=e,
                )
            )

    async def _handle_channel_metadata(
        self,
        event: ChannelMetadataChanged,
    ) -> None:
        """2026-08-27 v1.4.0 PR #14:频道元数据增量更新落库。

        `updateChannel` 推时 `channel_id` 已就绪,直接落库;`updateSupergroup`
        推时只有 `supergroup_id`,需通过 username 反查 channel_id
        (storage 现存 — InMemory/Jsonl/Postgres/Mongo 都按 username 找)。

        2026-09-04 v1.6.4:同时透传 4 个 spammer 过滤字段
        (`is_verified` / `is_scam` / `is_fake` / `has_protected_content`)。
        `None` 字段保持现有 partial-update 语义(不动该列)。
        """
        try:
            if event.channel_id == 0 and event.supergroup_id is not None:
                # supergroup_id → username(由 storage 反查)。无图源,
                # 实际生效路径:username 相同 → 找到 channel → 落库。
                # 这里走简单策略:用 storage.list_channels() 扫描找匹配的
                # username。生产可加索引;MVP 接受 O(N) 扫。
                # v1.4.0 MVP:仅 username 不为 None 时反查。
                if event.username:
                    for c in await self.storage.list_channels():
                        if c.username == event.username:
                            await self.storage.update_channel_metadata(
                                c.id,
                                title=event.title,
                                username=event.username,
                                member_count=event.member_count,
                                # 2026-09-04 v1.6.4:4 个 spammer 字段,
                                # partial-update 语义由 storage 抽象保证
                                is_verified=event.is_verified,
                                is_scam=event.is_scam,
                                is_fake=event.is_fake,
                                has_protected_content=event.has_protected_content,
                            )
                            log.debug(
                                "channel metadata updated via supergroup: "
                                "channel=%s member_count=%s",
                                c.id,
                                event.member_count,
                            )
                            return
                # 没找到匹配 username → 静默(可能 TDLib 推了陌生 supergroup)
                return
            if event.channel_id == 0:
                return  # 无 channel_id 又无 username,跳过
            await self.storage.update_channel_metadata(
                event.channel_id,
                title=event.title,
                username=event.username,
                member_count=event.member_count,
                # 2026-09-04 v1.6.4:4 个 spammer 字段
                is_verified=event.is_verified,
                is_scam=event.is_scam,
                is_fake=event.is_fake,
                has_protected_content=event.has_protected_content,
            )
            log.debug(
                "channel metadata updated: channel=%s title=%r username=%r "
                "member_count=%s verified=%s scam=%s fake=%s has_protected=%s",
                event.channel_id,
                event.title,
                event.username,
                event.member_count,
                event.is_verified,
                event.is_scam,
                event.is_fake,
                event.has_protected_content,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("handle ChannelMetadataChanged failed: %s", e)
            await self.bus.publish(
                ErrorOccurred(
                    source="monitor.channel_metadata",
                    message=str(e),
                    exception=e,
                )
            )

    async def _handle_channel_title_changed(
        self,
        event: ChannelTitleChanged,
    ) -> None:
        """2026-09-03 v1.6.0 PR #Q2:`updateChatTitle` → 落库 title。

        UI 端 `ChannelListCard` 同时订阅此事件实时刷卡片;此处只负责
        storage 持久化。不存在 channel 时 idempotent 不抛(TDLib 偶发对
        陈年 chat 推 metadata update,落库时机晚于本 update)。
        """
        try:
            await self.storage.update_channel_metadata(event.channel_id, title=event.new_title)
            log.debug(
                "channel title updated: channel=%s new_title=%r",
                event.channel_id,
                event.new_title,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("handle ChannelTitleChanged failed: %s", e)
            await self.bus.publish(
                ErrorOccurred(
                    source="monitor.channel_title",
                    message=str(e),
                    exception=e,
                )
            )

    async def _handle_channel_photo_changed(
        self,
        event: ChannelPhotoChanged,
    ) -> None:
        """2026-09-03 v1.6.0 PR #Q2:`updateChatPhoto` → 落库 photo_local_key。

        `event.local_path`:
        - 非空 str → 头像本地缓存路径,落库覆盖旧值
        - 空字符串 `""` → 头像被删,**显式**写 NULL 进 storage
        - None → 不动字段(等价于其他字段 None 语义;COALESCE 兜底)

        三态区分让 TDLib 推送「删头像」与「不动头像」可被观测。
        UI 端 `ChannelListCard` 订阅此事件实时刷图标。
        """
        try:
            if event.local_path is None:
                # None = 「不动」(与既有 partial update None 语义对齐)
                log.debug("channel photo no-op: channel=%s", event.channel_id)
                return
            # 非 None(包含空串):显式写真值。
            # 空串 "" → 视为「头像被删」→ 写 NULL;非空 → 写真路径。
            photo_to_set: str | None = event.local_path if event.local_path else None
            await self.storage.update_channel_metadata(
                event.channel_id, photo_local_key=photo_to_set
            )
            log.debug(
                "channel photo updated: channel=%s local_path=%r -> %r",
                event.channel_id,
                event.local_path,
                photo_to_set,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("handle ChannelPhotoChanged failed: %s", e)
            await self.bus.publish(
                ErrorOccurred(
                    source="monitor.channel_photo",
                    message=str(e),
                    exception=e,
                )
            )
