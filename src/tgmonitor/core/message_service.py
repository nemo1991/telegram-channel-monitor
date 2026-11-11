"""MessageService — 消息查询 / 标注域服务。

2026-11-11 v2.0.0 Phase 3:从 `SubscriptionService` 拆出消息查询,
合并 AppService 内联的收藏 / 标签 / 备注标注 + 单条删除。

职责:
  - `list_messages`(查 storage;channel_ids=None 时拉已订真理)
  - `set_favorite` / `set_tags` / `set_notes`(写 storage + publish MessageEdited)
  - `list_favorites` / `list_by_tag`(标注查询)
  - `delete_message`(单条删除,转 MonitorService — 含孤儿 bytes 清理)

设计:持 `bus + storage + monitor`;monitor 可选(仅 delete_message 用)。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from tgmonitor.core.dto import MessageDTO
from tgmonitor.core.events import EventBus, MessageEdited
from tgmonitor.core.storage.repository import StorageRepository

if TYPE_CHECKING:
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)


class MessageService:
    """消息查询 + 收藏 / 标签 / 备注标注 + 单条删除。"""

    def __init__(
        self,
        bus: EventBus,
        storage: StorageRepository,
        monitor: MonitorService | None = None,
    ) -> None:
        """持 3 个依赖:bus(发 MessageEdited)+ storage + monitor(删除路径)。"""
        self._bus = bus
        self._storage = storage
        self._monitor = monitor

    # ---------- 查询 ----------

    async def list_messages(
        self,
        channel_ids: list[int] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int | None = 200,
        search: str = "",
        include_unsubscribed: bool = False,
        favorite_only: bool = False,
        tag_only: bool = False,
        pinned_only: bool = False,
    ) -> list[MessageDTO]:
        """查消息 — `channel_ids=None` 时走 storage「已订」真理(修 #B 双真理问题)。

        `channel_ids=None` 时从 storage 取**当前真理**:
          - `include_unsubscribed=False`(默认):走 `list_subscribed_channels`
          - `include_unsubscribed=True`:走 `list_channels()` 拉所有 storage
            有记录的频道(含已退订)— 跨频道聚合搜索(SearchBar scope toggle)
        不用 in-memory cache — 跟 `list_subscribed_channels()` 是同一个真理。

        `search` 子串过滤 text + media.file_name,空 = 不过滤;
        `favorite_only` / `tag_only` / `pinned_only` 透传 storage(AND 语义)。
        """
        if channel_ids is None:
            if include_unsubscribed:
                all_channels = await self._storage.list_channels()
                channel_ids = [c.id for c in all_channels]
            else:
                channel_ids = [c.id for c in await self._storage.list_subscribed_channels()]
        if not channel_ids:
            return []
        return await self._storage.list_messages(
            channel_ids,
            date_from,
            date_to,
            limit,
            search=search,
            favorite_only=favorite_only,
            tag_only=tag_only,
            pinned_only=pinned_only,
        )

    async def list_favorites(self) -> list[MessageDTO]:
        """列出所有收藏消息 — 走 storage.list_favorites。"""
        return await self._storage.list_favorites()

    async def list_by_tag(self, tag: str) -> list[MessageDTO]:
        """按标签查消息 — 走 storage.list_by_tag。"""
        return await self._storage.list_by_tag(tag)

    # ---------- 标注(写 storage + publish MessageEdited)----------

    async def set_favorite(self, channel_id: int, telegram_msg_id: int, value: bool) -> None:
        """单条消息设收藏 — `MessageEdited` 触发 UI 行首 ★ icon 刷新。"""
        await self._storage.set_favorite(channel_id, telegram_msg_id, value)
        await self._publish_edited(channel_id, telegram_msg_id)

    async def set_tags(self, channel_id: int, telegram_msg_id: int, tags: list[str]) -> None:
        """单条消息设标签 — 写本地 storage + publish MessageEdited。"""
        await self._storage.set_tags(channel_id, telegram_msg_id, tags)
        await self._publish_edited(channel_id, telegram_msg_id)

    async def set_notes(self, channel_id: int, telegram_msg_id: int, notes: str) -> None:
        """单条消息设备注 — 写本地 storage + publish MessageEdited。"""
        await self._storage.set_notes(channel_id, telegram_msg_id, notes)
        await self._publish_edited(channel_id, telegram_msg_id)

    async def _publish_edited(self, channel_id: int, telegram_msg_id: int) -> None:
        """标注写回后重读消息并发 MessageEdited;读失败仅跳过(标注已落库)。"""
        try:
            msg = await self._storage.get_message(channel_id, telegram_msg_id)
        except Exception:  # noqa: BLE001
            msg = None
        if msg is not None:
            await self._bus.publish(MessageEdited(message=msg))

    # ---------- 删除 ----------

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """单条消息删除 — 转 MonitorService(落库删 row + 孤儿 bytes 清理)。

        MessageDetail 顶部「删除」按钮 + 右键单条菜单调用入口。
        monitor 在启动后必非 None(UI 触发 = 已登录),assert 锁死契约。
        """
        assert self._monitor is not None
        await self._monitor.delete_message(channel_id, telegram_msg_id)
