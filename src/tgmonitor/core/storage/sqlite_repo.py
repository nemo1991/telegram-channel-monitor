"""SQLite 仓储 facade — v2.0.0 默认后端(aiosqlite,WAL)。

2026-11-25 P2-N1:按域把方法体抽到 7 个 `_xxx_sqlite.py` free-function module
(与 PG 端 `_xxx_pg.py` 1:1 对称)。本类只持连接状态(`_db` / `_lock` / `_path`)
并提供 48 个 1 行 thin delegate;查询语义与 `postgres_repo` 逐条对齐(契约测试
`tests/test_storage_backends.py` parametrize 守门)。

设计要点:
- 单连接 + `asyncio.Lock` 串行化所有操作。SQLite 单写者,桌面规模下
  串行无性能问题;锁同时避免「同连接读到未提交事务中间态」。
- `isolation_level=None`(autocommit);`save_message` 显式 BEGIN/COMMIT。
- WAL + `foreign_keys=ON` + `busy_timeout=5000` + `synchronous=NORMAL`。
- datetime 序列化:aware → UTC ISO 字符串,naive → 原样 ISO;读回
  `datetime.fromisoformat`(naive/aware 各自还原,round-trip `==` 成立)。
  排序按 ISO 字符串字典序 — 全 naive(测试)或全 UTC(生产)时与 PG
  timestamptz 语义一致;naive/aware 混存的极端场景不做保证。
- `tags` 用 JSON 数组文本(JSON1 `json_each` / `json_array_length` 查询),
  `raw` / `forward_origin` / `reactions` 用 JSON 文本。
- `LOWER` 注册为 Python `str.lower`(Unicode 感知),对齐 PG 的
  Unicode LOWER(SQLite 内置 LOWER 只折 ASCII)。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from tgmonitor.core.dto import (
    ActivityDTO,
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
from tgmonitor.core.storage import _activity_sqlite as _activity
from tgmonitor.core.storage import _channels_sqlite as _channels
from tgmonitor.core.storage import _lifecycle_sqlite as _lifecycle
from tgmonitor.core.storage import _media_sqlite as _media
from tgmonitor.core.storage import _messages_sqlite as _messages
from tgmonitor.core.storage import _thumbnails_sqlite as _thumbnails
from tgmonitor.core.storage import _user_meta_sqlite as _user_meta
from tgmonitor.core.storage._sqlite_common import (  # noqa: F401 — re-export 保持契约
    EXPECTED_SCHEMA,
    EXPECTED_TABLES,
    SCHEMA_SQL,
    _dt_from_db,
    _dt_to_db,
    _json_from_db,
    _json_to_db,
    _media_to_row,
    _media_where_clause,
    _placeholders,
    _row_to_channel,
    _row_to_media,
    _row_to_message,
    _row_to_thumbnail,
    _sql_lower,
)
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.storage.schema_report import SchemaReport


class SqliteRepository(StorageRepository):
    """SQLite 后端 — 单连接 + asyncio.Lock 串行化;WAL 模式。

    facade:连接状态在此,方法体在 7 个按域 module(见顶部 import)。
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path) if path != ":memory:" else path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ---- 生命周期 → _lifecycle_sqlite ----

    async def connect(self) -> None:
        return await _lifecycle.connect(self)

    async def close(self) -> None:
        return await _lifecycle.close(self)

    async def init_schema(self) -> None:
        return await _lifecycle.init_schema(self)

    async def introspect_schema(self) -> SchemaReport:
        return await _lifecycle.introspect_schema(self)

    async def repair_schema(self, report: SchemaReport) -> None:
        return await _lifecycle.repair_schema(self, report)

    async def ping(self) -> bool:
        return await _lifecycle.ping(self)

    # ---- 频道 + 元数据 → _channels_sqlite ----

    async def upsert_channel(self, channel: ChannelDTO) -> None:
        return await _channels.upsert_channel(self, channel)

    async def upsert_channel_metadata(self, channel: ChannelDTO) -> None:
        return await _channels.upsert_channel_metadata(self, channel)

    async def update_channel_metadata(
        self,
        channel_id: int,
        *,
        title: str | None = None,
        username: str | None = None,
        member_count: int | None = None,
        photo_local_key: str | None = None,
        is_verified: bool | None = None,
        is_scam: bool | None = None,
        is_fake: bool | None = None,
        has_protected_content: bool | None = None,
    ) -> None:
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

    async def set_channel_monitored(self, channel_id: int, monitored: bool) -> None:
        return await _channels.set_channel_monitored(self, channel_id, monitored)

    async def set_channel_tg_subscribed(self, channel_id: int, tg_subscribed: bool) -> None:
        return await _channels.set_channel_tg_subscribed(self, channel_id, tg_subscribed)

    async def upsert_channel_and_set_monitored(self, channel: ChannelDTO) -> None:
        """P2 #37:原子 upsert + 置监听 — thin delegate → `_channels_sqlite`."""
        return await _channels.upsert_channel_and_set_monitored(self, channel)

    async def upsert_channel_metadata_and_set_tg_subscribed(self, channel: ChannelDTO) -> None:
        """P2 #37:原子 metadata upsert + 置 TG 订阅 — thin delegate."""
        return await _channels.upsert_channel_metadata_and_set_tg_subscribed(self, channel)

    async def set_channel_flags(
        self,
        channel_id: int,
        *,
        monitored: bool | None = None,
        tg_subscribed: bool | None = None,
    ) -> None:
        """P2 #37:双标志单事务设置 — thin delegate → `_channels_sqlite`."""
        return await _channels.set_channel_flags(
            self, channel_id, monitored=monitored, tg_subscribed=tg_subscribed
        )

    async def count_tg_subscribed(self) -> int:
        return await _channels.count_tg_subscribed(self)

    async def list_tg_subscribed_channels(self) -> list[ChannelDTO]:
        return await _channels.list_tg_subscribed_channels(self)

    async def list_channels(self) -> list[ChannelDTO]:
        return await _channels.list_channels(self)

    async def list_monitored_channels(self) -> list[ChannelDTO]:
        return await _channels.list_monitored_channels(self)

    async def get_channel(self, channel_id: int) -> ChannelDTO | None:
        return await _channels.get_channel(self, channel_id)

    async def delete_channel(self, channel_id: int) -> None:
        return await _channels.delete_channel(self, channel_id)

    async def get_max_telegram_msg_id(self, channel_id: int) -> int | None:
        return await _channels.get_max_telegram_msg_id(self, channel_id)

    async def get_meta(self, key: str) -> str | None:
        return await _channels.get_meta(self, key)

    async def set_meta(self, key: str, value: str) -> None:
        return await _channels.set_meta(self, key, value)

    # ---- 消息 → _messages_sqlite ----

    async def save_message(self, message: MessageDTO) -> int:
        return await _messages.save_message(self, message)

    async def update_message(self, message: MessageDTO) -> None:
        return await _messages.update_message(self, message)

    async def update_message_media(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media: list[MediaDTO],
    ) -> None:
        """P2 #36:定向重写 media 行 — thin delegate → `_messages_sqlite`."""
        return await _messages.update_message_media(self, channel_id, telegram_msg_id, media)

    async def update_message_interactions(
        self,
        channel_id: int,
        telegram_msg_id: int,
        *,
        views: int | None = None,
        reactions: list[Any] | None = None,
    ) -> None:
        return await _messages.update_message_interactions(
            self,
            channel_id,
            telegram_msg_id,
            views=views,
            reactions=reactions,
        )

    async def update_message_pin(
        self,
        channel_id: int,
        telegram_msg_id: int,
        is_pinned: bool,
    ) -> None:
        return await _messages.update_message_pin(self, channel_id, telegram_msg_id, is_pinned)

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        return await _messages.delete_message(self, channel_id, telegram_msg_id)

    async def delete_messages(self, channel_id: int, msg_ids: list[int]) -> None:
        return await _messages.delete_messages(self, channel_id, msg_ids)

    async def get_message(self, channel_id: int, telegram_msg_id: int) -> MessageDTO | None:
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
        tag: str = "",
    ) -> list[MessageDTO]:
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
            tag=tag,
        )

    async def count_messages(self, channel_id: int) -> int:
        return await _messages.count_messages(self, channel_id)

    async def aggregate_per_channel(self, channel_ids: list[int]) -> dict[int, ChannelStats]:
        return await _messages.aggregate_per_channel(self, channel_ids)

    # ---- 媒体 → _media_sqlite ----

    async def find_media_by_file_id(self, telegram_file_id: str) -> MediaDTO | None:
        return await _media.find_media_by_file_id(self, telegram_file_id)

    async def list_media(
        self,
        *,
        channel_ids: list[int] | None = None,
        status: MediaDownloadStatus | None = None,
        media_type: MediaType | None = None,
        search: str = "",
        undownloaded: bool = False,
        limit: int = 1000,
        offset: int = 0,
        sort: SortKey = SortKey.DATE,
        sort_dir: SortDir = SortDir.DESC,
    ) -> list[tuple[MessageDTO, int, MediaDTO]]:
        return await _media.list_media(
            self,
            channel_ids=channel_ids,
            status=status,
            media_type=media_type,
            search=search,
            undownloaded=undownloaded,
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
        undownloaded: bool = False,
    ) -> int:
        return await _media.count_media(
            self,
            channel_ids=channel_ids,
            status=status,
            media_type=media_type,
            search=search,
            undownloaded=undownloaded,
        )

    async def count_media_by_object_key(self, object_key: str) -> int:
        return await _media.count_media_by_object_key(self, object_key)

    async def count_media_by_channel(self, channel_id: int) -> int:
        return await _media.count_media_by_channel(self, channel_id)

    async def reset_stuck_downloads(self) -> int:
        return await _media.reset_stuck_downloads(self)

    # ---- 缩略图 → _thumbnails_sqlite ----

    async def save_thumbnail(self, thumb: ThumbnailDTO) -> None:
        return await _thumbnails.save_thumbnail(self, thumb)

    async def get_thumbnail(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> ThumbnailDTO | None:
        return await _thumbnails.get_thumbnail(self, channel_id, telegram_msg_id, media_idx)

    async def delete_thumbnail(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        return await _thumbnails.delete_thumbnail(self, channel_id, telegram_msg_id, media_idx)

    async def list_thumbnails_for_message(
        self,
        channel_id: int,
        telegram_msg_id: int,
    ) -> list[ThumbnailDTO]:
        return await _thumbnails.list_thumbnails_for_message(self, channel_id, telegram_msg_id)

    async def count_thumbnails_by_object_key(self, object_key: str) -> int:
        return await _thumbnails.count_thumbnails_by_object_key(self, object_key)

    # ---- 用户元数据 → _user_meta_sqlite ----

    async def set_favorite(self, channel_id: int, telegram_msg_id: int, value: bool) -> None:
        return await _user_meta.set_favorite(self, channel_id, telegram_msg_id, value)

    async def set_tags(self, channel_id: int, telegram_msg_id: int, tags: list[str]) -> None:
        return await _user_meta.set_tags(self, channel_id, telegram_msg_id, tags)

    async def set_notes(self, channel_id: int, telegram_msg_id: int, notes: str) -> None:
        return await _user_meta.set_notes(self, channel_id, telegram_msg_id, notes)

    async def list_favorites(self) -> list[MessageDTO]:
        return await _user_meta.list_favorites(self)

    async def list_by_tag(self, tag: str) -> list[MessageDTO]:
        return await _user_meta.list_by_tag(self, tag)

    # ---- 活动记录 → _activity_sqlite ----

    async def append_activity(self, activity: ActivityDTO) -> None:
        return await _activity.append_activity(self, activity)

    async def list_activities(self, limit: int = 200) -> list[ActivityDTO]:
        return await _activity.list_activities(self, limit)
