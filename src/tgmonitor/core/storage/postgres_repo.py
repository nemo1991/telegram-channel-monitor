"""PostgreSQL 实现 — `asyncpg`。

- 显式 schema(内联 `SCHEMA_SQL`,2026-10-12 plan-batch3 目标 3 step 3.1
  从 `storage/schema.sql` 文件 inline;保留 `EXPECTED_SCHEMA` / `EXPECTED_TABLES`
  同源对照供 `introspect_schema()` 比对,两处统一改)
- JSON 列(`raw`):用 `json.dumps` / `jsonb` 类型
- 唯一约束 `(channel_id, telegram_msg_id)` 配合 ON CONFLICT 实现幂等 upsert
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import asyncpg

from tgmonitor.core.dto import (
    ChannelDTO,
    ChannelStats,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    SortDir,
    SortKey,
    ThumbnailDTO,
)
from tgmonitor.core.storage import _channels_pg as _channels
from tgmonitor.core.storage import _lifecycle_pg as _lifecycle
from tgmonitor.core.storage import _media_pg as _media
from tgmonitor.core.storage import _messages_pg as _messages
from tgmonitor.core.storage import _thumbnails_pg as _thumbnails
from tgmonitor.core.storage import _user_meta_pg as _user_meta
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.storage.schema_report import SchemaReport

log = logging.getLogger(__name__)

# 2026-10-16 plan-batch8-1.1:`SCHEMA_SQL` / `EXPECTED_SCHEMA` / `EXPECTED_TABLES`
# 与 6 个 lifecycle method 下沉到 `_lifecycle_pg.py`,facade 改 1 行 thin
# delegate。
# 2026-10-16 plan-batch8-1.2:5 个 user_meta method 下沉到 `_user_meta_pg.py`。
# 2026-10-16 plan-batch8-1.3:11 个 channel + meta method + `_row_to_channel` 下沉到
# `_channels_pg.py`。
# 2026-10-16 plan-batch8-1.4:5 个 thumbnail method + `_row_to_thumbnail` 下沉到
# `_thumbnails_pg.py`。
# 2026-10-19 plan-batch8-1.5:5 个 media method + `_MEDIA_SORT_COLUMN` /
# `_media_status` / `_media_to_row` / `_row_to_media` / `_media_where_clause`
# 下沉到 `_media_pg.py`。
# 2026-10-19 plan-batch8-1.6:10 个 message method + `_row_to_message` 下沉到
# `_messages_pg.py`。facade 主体(43 method 全部 1 行 thin delegate)目标达成。


class PostgresRepository(StorageRepository):
    """PostgreSQL 实现 — `asyncpg` 连接池 + ON CONFLICT 幂等 upsert。

    media 是独立表 + FK CASCADE;ON CONFLICT 配合 (channel_id, telegram_msg_id)
    唯一约束实现 save_message 幂等。
    """

    def __init__(self, dsn: str) -> None:
        """`dsn` = asyncpg DSN;实际 pool 在 connect() 时建。"""
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    # ============================================================
    # ---- A. 生命周期(2026-10-16 plan-batch8-1.1) ----
    # ============================================================

    async def connect(self) -> None:
        """建 asyncpg 连接池 — thin delegate → `_lifecycle_pg`."""
        return await _lifecycle.connect(self)

    async def close(self) -> None:
        """关连接池 — thin delegate → `_lifecycle_pg`."""
        return await _lifecycle.close(self)

    async def init_schema(self) -> None:
        """执行 SCHEMA_SQL(创建表 + 索引)— thin delegate → `_lifecycle_pg`."""
        return await _lifecycle.init_schema(self)

    async def introspect_schema(self) -> SchemaReport:
        """查 information_schema 比对 EXPECTED_SCHEMA — thin delegate."""
        return await _lifecycle.introspect_schema(self)

    async def repair_schema(self, report: SchemaReport) -> None:
        """基于 report 跑幂等修复 — thin delegate → `_lifecycle_pg`."""
        return await _lifecycle.repair_schema(self, report)

    async def ping(self) -> bool:
        """SELECT 1 探活 — thin delegate → `_lifecycle_pg`."""
        return await _lifecycle.ping(self)

    # ---- 用户元数据(2026-09-09 v1.7.2,2026-10-16 plan-batch8-1.2) ----

    async def set_favorite(self, channel_id: int, telegram_msg_id: int, value: bool) -> None:
        """2026-09-09 v1.7.2:单条设 `is_favorite` — thin delegate → `_user_meta_pg`."""
        return await _user_meta.set_favorite(self, channel_id, telegram_msg_id, value)

    async def set_tags(self, channel_id: int, telegram_msg_id: int, tags: list[str]) -> None:
        """2026-09-09 v1.7.2:单条覆盖式设 `tags` — thin delegate → `_user_meta_pg`."""
        return await _user_meta.set_tags(self, channel_id, telegram_msg_id, tags)

    async def set_notes(self, channel_id: int, telegram_msg_id: int, notes: str) -> None:
        """2026-09-09 v1.7.2:单条覆盖式设 `notes` — thin delegate → `_user_meta_pg`."""
        return await _user_meta.set_notes(self, channel_id, telegram_msg_id, notes)

    async def list_favorites(self) -> list[MessageDTO]:
        """2026-09-09 v1.7.2:列所有 `is_favorite=TRUE` 消息 — thin delegate → `_user_meta_pg`."""
        return await _user_meta.list_favorites(self)

    async def list_by_tag(self, tag: str) -> list[MessageDTO]:
        """2026-09-09 v1.7.2:按 `tag` 精确匹配查询 — thin delegate → `_user_meta_pg`."""
        return await _user_meta.list_by_tag(self, tag)

    # ---- 频道(2026-10-16 plan-batch8-1.3) ----

    async def upsert_channel(self, channel: ChannelDTO) -> None:
        """thin delegate → `_channels_pg`."""
        return await _channels.upsert_channel(self, channel)

    async def upsert_channel_metadata(self, channel: ChannelDTO) -> None:
        """thin delegate → `_channels_pg`."""
        return await _channels.upsert_channel_metadata(self, channel)

    async def update_channel_metadata(
        self,
        channel_id: int,
        *,
        title: str | None = None,
        username: str | None = None,
        member_count: int | None = None,
        photo_local_key: str | None = None,
        is_verified: bool | None = None,  # 2026-09-04 v1.6.4
        is_scam: bool | None = None,
        is_fake: bool | None = None,
        has_protected_content: bool | None = None,
    ) -> None:
        """thin delegate → `_channels_pg`."""
        return await _channels.update_channel_metadata(
            self,
            channel_id,
            title=title,
            username=username,
            member_count=member_count,
            photo_local_key=photo_local_key,
            is_verified=is_verified,
            is_scam=is_scam,
            is_fake=is_fake,
            has_protected_content=has_protected_content,
        )

    async def set_channel_subscribed(self, channel_id: int, subscribed: bool) -> None:
        """thin delegate → `_channels_pg`."""
        return await _channels.set_channel_subscribed(self, channel_id, subscribed)

    async def list_channels(self) -> list[ChannelDTO]:
        """thin delegate → `_channels_pg`."""
        return await _channels.list_channels(self)

    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        """thin delegate → `_channels_pg`."""
        return await _channels.list_subscribed_channels(self)

    async def get_channel(self, channel_id: int) -> ChannelDTO | None:
        """thin delegate → `_channels_pg`."""
        return await _channels.get_channel(self, channel_id)

    async def delete_channel(self, channel_id: int) -> None:
        """thin delegate → `_channels_pg`."""
        return await _channels.delete_channel(self, channel_id)

    async def get_max_telegram_msg_id(self, channel_id: int) -> int | None:
        """thin delegate → `_channels_pg`."""
        return await _channels.get_max_telegram_msg_id(self, channel_id)

    async def get_meta(self, key: str) -> str | None:
        """thin delegate → `_channels_pg`."""
        return await _channels.get_meta(self, key)

    async def set_meta(self, key: str, value: str) -> None:
        """thin delegate → `_channels_pg`."""
        return await _channels.set_meta(self, key, value)

    # ---- 消息(2026-10-19 plan-batch8-1.6) ----

    async def save_message(self, message: MessageDTO) -> int:
        """幂等 upsert:返回 messages.id — thin delegate → `_messages_pg`."""
        return await _messages.save_message(self, message)

    async def update_message(self, message: MessageDTO) -> None:
        """代理到 save_message(upsert 语义一致)— thin delegate."""
        return await _messages.update_message(self, message)

    async def update_message_interactions(
        self,
        channel_id: int,
        telegram_msg_id: int,
        *,
        views: int | None = None,
        reactions: list[Any] | None = None,
    ) -> None:
        """增量更新 views / reactions(JSONB)— thin delegate → `_messages_pg`."""
        return await _messages.update_message_interactions(
            self, channel_id, telegram_msg_id, views=views, reactions=reactions
        )

    async def update_message_pin(
        self,
        channel_id: int,
        telegram_msg_id: int,
        is_pinned: bool,
    ) -> None:
        """PG 单条 UPDATE 设 is_pinned — thin delegate → `_messages_pg`."""
        return await _messages.update_message_pin(self, channel_id, telegram_msg_id, is_pinned)

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """删单条消息;media 行 FK CASCADE 自动删 — thin delegate."""
        return await _messages.delete_message(self, channel_id, telegram_msg_id)

    async def delete_messages(self, channel_id: int, msg_ids: list[int]) -> None:
        """批量删单频道 N 条消息;media 行 FK CASCADE 自动删 — thin delegate."""
        return await _messages.delete_messages(self, channel_id, msg_ids)

    async def get_message(self, channel_id: int, telegram_msg_id: int) -> MessageDTO | None:
        """单条消息 + 关联 media;不存在返 None — thin delegate → `_messages_pg`."""
        return await _messages.get_message(self, channel_id, telegram_msg_id)

    async def list_messages(
        self,
        channel_ids: list[int],
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        search: str = "",
        favorite_only: bool = False,
        tag_only: bool = False,
        pinned_only: bool = False,
    ) -> list[MessageDTO]:
        """按时间升序 + id 升序(与 Mongo 对齐);media 二次查询拼回 — thin delegate."""
        return await _messages.list_messages(
            self,
            channel_ids,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
            search=search,
            favorite_only=favorite_only,
            tag_only=tag_only,
            pinned_only=pinned_only,
        )

    async def count_messages(self, channel_id: int) -> int:
        """该频道已落库消息数;COUNT(*) 聚合 — thin delegate → `_messages_pg`."""
        return await _messages.count_messages(self, channel_id)

    async def aggregate_per_channel(self, channel_ids: list[int]) -> dict[int, ChannelStats]:
        """单 GROUP BY 一次性拿 4 字段(messages / media / done_media / last_date) — thin delegate."""
        return await _messages.aggregate_per_channel(self, channel_ids)

    # ---- 媒体(2026-08-25 PR #3,2026-10-19 plan-batch8-1.5) ----

    async def find_media_by_file_id(self, telegram_file_id: str) -> MediaDTO | None:
        """跨频道去重:任一已 DONE 的同 file_id media → 返 DTO — thin delegate."""
        return await _media.find_media_by_file_id(self, telegram_file_id)

    async def list_media(
        self,
        *,
        channel_ids: list[int] | None = None,
        status: MediaDownloadStatus | None = None,
        media_type: MediaType | None = None,
        search: str = "",
        limit: int = 1000,
        offset: int = 0,
        sort: SortKey = SortKey.DATE,
        sort_dir: SortDir = SortDir.DESC,
    ) -> list[tuple[MessageDTO, int, MediaDTO]]:
        """messages JOIN media + ROW_NUMBER 窗口函数 — thin delegate → `_media_pg`."""
        return await _media.list_media(
            self,
            channel_ids=channel_ids,
            status=status,
            media_type=media_type,
            search=search,
            limit=limit,
            offset=offset,
            sort=sort,
            sort_dir=sort_dir,
        )

    async def count_media(
        self,
        *,
        channel_ids: list[int] | None = None,
        status: MediaDownloadStatus | None = None,
        media_type: MediaType | None = None,
        search: str = "",
    ) -> int:
        """与 list_media 同 filter — thin delegate → `_media_pg`."""
        return await _media.count_media(
            self,
            channel_ids=channel_ids,
            status=status,
            media_type=media_type,
            search=search,
        )

    async def count_media_by_object_key(self, object_key: str) -> int:
        """refcount — thin delegate → `_media_pg`."""
        return await _media.count_media_by_object_key(self, object_key)

    async def count_media_by_channel(self, channel_id: int) -> int:
        """该频道全部 media 数 — thin delegate → `_media_pg`."""
        return await _media.count_media_by_channel(self, channel_id)

    # ---- 缩略图(2026-09-29 独立存储,2026-10-16 plan-batch8-1.4) ----

    async def save_thumbnail(self, thumb: ThumbnailDTO) -> None:
        """thin delegate → `_thumbnails_pg`."""
        return await _thumbnails.save_thumbnail(self, thumb)

    async def get_thumbnail(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> ThumbnailDTO | None:
        """thin delegate → `_thumbnails_pg`."""
        return await _thumbnails.get_thumbnail(self, channel_id, telegram_msg_id, media_idx)

    async def delete_thumbnail(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        """thin delegate → `_thumbnails_pg`."""
        return await _thumbnails.delete_thumbnail(
            self, channel_id, telegram_msg_id, media_idx
        )

    async def list_thumbnails_for_message(
        self,
        channel_id: int,
        telegram_msg_id: int,
    ) -> list[ThumbnailDTO]:
        """thin delegate → `_thumbnails_pg`."""
        return await _thumbnails.list_thumbnails_for_message(self, channel_id, telegram_msg_id)

    async def count_thumbnails_by_object_key(self, object_key: str) -> int:
        """thin delegate → `_thumbnails_pg`."""
        return await _thumbnails.count_thumbnails_by_object_key(self, object_key)
