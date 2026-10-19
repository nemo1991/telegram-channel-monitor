"""MongoDB 实现 — `motor`(官方 async 驱动)。

- 集合:channels / messages / media / meta
- 唯一索引 `{channel_id, telegram_msg_id}`
- 查询语义与 PostgresRepository 对齐(按 `date ASC, _id ASC` 排序)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

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
from tgmonitor.core.storage import _channels_mongo as _channels
from tgmonitor.core.storage import _lifecycle_mongo as _lifecycle
from tgmonitor.core.storage import _media_mongo as _media
from tgmonitor.core.storage import _messages_mongo as _messages
from tgmonitor.core.storage import _thumbnails_mongo as _thumbnails
from tgmonitor.core.storage import _user_meta_mongo as _user_meta
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.storage.schema_report import SchemaReport

# 2026-10-16 plan-batch8-1.1:6 个 lifecycle method + `from_client_factory` 下沉到
# `_lifecycle_mongo.py`;`_db` property / classmethod `from_client` 留 facade。
# 2026-10-16 plan-batch8-1.2:5 个 user_meta method 下沉到 `_user_meta_mongo.py`。
# 2026-10-16 plan-batch8-1.3:11 个 channel + meta method + `_doc_to_channel` 下沉到
# `_channels_mongo.py`。
# 2026-10-16 plan-batch8-1.4:5 个 thumbnail method + `_thumb_to_doc` /
# `_doc_to_thumb` 下沉到 `_thumbnails_mongo.py`。
# 2026-10-19 plan-batch8-1.5:5 个 media method + `_media_status` /
# `_media_to_doc` / `_doc_to_media` / `_MEDIA_SORT_FIELD` / `_build_media_pipeline`
# 下沉到 `_media_mongo.py`。
# 2026-10-19 plan-batch8-1.6:10 个 message method + `_doc_to_message` +
# `_next_message_id` 下沉到 `_messages_mongo.py`。facade 主体(43 method 全部
# 1 行 thin delegate + `__init__` + `from_client` classmethod + `db` property)
# 目标达成。`_escape_regex` 仍留 facade(只 list_media 跨子用,经由
# `_build_media_pipeline` 间接复用)。`Mapping` import 1.6 后仍保留(aggregate
# pipeline 类型注解用)。


class MongoRepository(StorageRepository):
    """`_id` 用 `ObjectId`;`id` 字段对 messages 是 ObjectId 的字符串形式。"""

    def __init__(self, dsn: str, database: str = "tgmonitor") -> None:
        """`dsn` = motor DSN;`database` = 库名(默认 `tgmonitor`)。"""
        self._dsn = dsn
        self._db_name = database
        self._client: AsyncIOMotorClient | None = None
        self._db: AsyncIOMotorDatabase | None = None

    @classmethod
    def from_client(
        cls,
        client: AsyncIOMotorClient,
        database: str,
    ) -> MongoRepository:
        """2026-08-31 v1.5.0 PR #A7:用预构造 client 建实例 — 集成测试用。

        生产代码仍走 `__init__(dsn, database)`,走 `connect()` 自建 motor client。
        测试(mongomock_motor / 真 testcontainers mongodb)可传已构造好的对象,
        跳过 motor 的 DSN 解析。

        2026-10-16 plan-batch8-1.1:实现下沉到 `_lifecycle_mongo.from_client_factory`,
        facade 仍保留 classmethod 转调以保 4 个 test 文件 + conftest 调用路径
        零改动。
        """
        return _lifecycle.from_client_factory(client, database)

    @property
    def db(self) -> AsyncIOMotorDatabase:
        """当前 DB 句柄;调用前必须 connect()。"""
        assert self._db is not None, "call connect() first"
        return self._db

    # ============================================================
    # ---- A. 生命周期(2026-10-16 plan-batch8-1.1) ----
    # ============================================================

    async def connect(self) -> None:
        """建 motor 客户端 + 拿 db 句柄 — thin delegate → `_lifecycle_mongo`."""
        return await _lifecycle.connect(self)

    async def close(self) -> None:
        """关 motor 客户端;`from_client` 路径不关 — thin delegate."""
        return await _lifecycle.close(self)

    async def init_schema(self) -> None:
        """建 6+ 索引 — thin delegate → `_lifecycle_mongo`."""
        return await _lifecycle.init_schema(self)

    async def introspect_schema(self) -> SchemaReport:
        """检查期望索引是否齐全 — thin delegate → `_lifecycle_mongo`."""
        return await _lifecycle.introspect_schema(self)

    async def repair_schema(self, report: SchemaReport) -> None:
        """重建缺失唯一索引 — thin delegate → `_lifecycle_mongo`."""
        return await _lifecycle.repair_schema(self, report)

    async def ping(self) -> bool:
        """`db.command("ping")` 探活 — thin delegate → `_lifecycle_mongo`."""
        return await _lifecycle.ping(self)

    # ---- 用户元数据(2026-09-09 v1.7.2,2026-10-16 plan-batch8-1.2) ----

    async def set_favorite(self, channel_id: int, telegram_msg_id: int, value: bool) -> None:
        """2026-09-09 v1.7.2:单条设 `is_favorite` — thin delegate → `_user_meta_mongo`."""
        return await _user_meta.set_favorite(self, channel_id, telegram_msg_id, value)

    async def set_tags(self, channel_id: int, telegram_msg_id: int, tags: list[str]) -> None:
        """2026-09-09 v1.7.2:单条覆盖式设 `tags` — thin delegate → `_user_meta_mongo`."""
        return await _user_meta.set_tags(self, channel_id, telegram_msg_id, tags)

    async def set_notes(self, channel_id: int, telegram_msg_id: int, notes: str) -> None:
        """2026-09-09 v1.7.2:单条覆盖式设 `notes` — thin delegate → `_user_meta_mongo`."""
        return await _user_meta.set_notes(self, channel_id, telegram_msg_id, notes)

    async def list_favorites(self) -> list[MessageDTO]:
        """2026-09-09 v1.7.2:列所有 `is_favorite=True` 消息 — thin delegate → `_user_meta_mongo`."""
        return await _user_meta.list_favorites(self)

    async def list_by_tag(self, tag: str) -> list[MessageDTO]:
        """2026-09-09 v1.7.2:按 `tag` 精确匹配查询 — thin delegate → `_user_meta_mongo`."""
        return await _user_meta.list_by_tag(self, tag)

    # ---- 频道(2026-10-16 plan-batch8-1.3) ----

    async def upsert_channel(self, channel: ChannelDTO) -> None:
        """thin delegate → `_channels_mongo`."""
        return await _channels.upsert_channel(self, channel)

    async def upsert_channel_metadata(self, channel: ChannelDTO) -> None:
        """thin delegate → `_channels_mongo`."""
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
        """thin delegate → `_channels_mongo`."""
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
        """thin delegate → `_channels_mongo`."""
        return await _channels.set_channel_subscribed(self, channel_id, subscribed)

    async def list_channels(self) -> list[ChannelDTO]:
        """thin delegate → `_channels_mongo`."""
        return await _channels.list_channels(self)

    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        """thin delegate → `_channels_mongo`."""
        return await _channels.list_subscribed_channels(self)

    async def get_channel(self, channel_id: int) -> ChannelDTO | None:
        """thin delegate → `_channels_mongo`."""
        return await _channels.get_channel(self, channel_id)

    async def delete_channel(self, channel_id: int) -> None:
        """thin delegate → `_channels_mongo`."""
        return await _channels.delete_channel(self, channel_id)

    async def get_max_telegram_msg_id(self, channel_id: int) -> int | None:
        """thin delegate → `_channels_mongo`."""
        return await _channels.get_max_telegram_msg_id(self, channel_id)

    async def get_meta(self, key: str) -> str | None:
        """thin delegate → `_channels_mongo`."""
        return await _channels.get_meta(self, key)

    async def set_meta(self, key: str, value: str) -> None:
        """thin delegate → `_channels_mongo`."""
        return await _channels.set_meta(self, key, value)

    # ---- 消息(2026-10-19 plan-batch8-1.6) ----

    async def save_message(self, message: MessageDTO) -> int:
        """幂等 upsert,显式 int 主键 — thin delegate → `_messages_mongo`."""
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
        """Mongo `$set` 部分更新 views / reactions — thin delegate → `_messages_mongo`."""
        return await _messages.update_message_interactions(
            self, channel_id, telegram_msg_id, views=views, reactions=reactions
        )

    async def update_message_pin(
        self,
        channel_id: int,
        telegram_msg_id: int,
        is_pinned: bool,
    ) -> None:
        """Mongo `$set` 单字段 is_pinned — thin delegate → `_messages_mongo`."""
        return await _messages.update_message_pin(self, channel_id, telegram_msg_id, is_pinned)

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """删单条消息 + media 子文档 + thumb — thin delegate → `_messages_mongo`."""
        return await _messages.delete_message(self, channel_id, telegram_msg_id)

    async def delete_messages(self, channel_id: int, msg_ids: list[int]) -> None:
        """批量删单频道 N 条 + thumb — thin delegate → `_messages_mongo`."""
        return await _messages.delete_messages(self, channel_id, msg_ids)

    async def get_message(self, channel_id: int, telegram_msg_id: int) -> MessageDTO | None:
        """单条消息 + media 子文档;不存在返 None — thin delegate → `_messages_mongo`."""
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
        """`$in` 走 channel_ids + date ASC — thin delegate → `_messages_mongo`."""
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
        """该频道已落库消息数;`count_documents` — thin delegate → `_messages_mongo`."""
        return await _messages.count_messages(self, channel_id)

    async def aggregate_per_channel(self, channel_ids: list[int]) -> dict[int, ChannelStats]:
        """$unwind + $group 单 pipeline 聚合 4 字段 — thin delegate → `_messages_mongo`."""
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
        """$unwind messages.media + $match + 排序 — thin delegate → `_media_mongo`."""
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
        """与 list_media 同 filter — thin delegate → `_media_mongo`."""
        return await _media.count_media(
            self,
            channel_ids=channel_ids,
            status=status,
            media_type=media_type,
            search=search,
        )

    async def count_media_by_object_key(self, object_key: str) -> int:
        """refcount — thin delegate → `_media_mongo`."""
        return await _media.count_media_by_object_key(self, object_key)

    async def count_media_by_channel(self, channel_id: int) -> int:
        """该频道全部 media 数 — thin delegate → `_media_mongo`."""
        return await _media.count_media_by_channel(self, channel_id)

    # ---- 缩略图(2026-09-29 独立存储,2026-10-16 plan-batch8-1.4) ----

    async def save_thumbnail(self, thumb: ThumbnailDTO) -> None:
        """thin delegate → `_thumbnails_mongo`."""
        return await _thumbnails.save_thumbnail(self, thumb)

    async def get_thumbnail(
        self, channel_id: int, telegram_msg_id: int, media_idx: int
    ) -> ThumbnailDTO | None:
        """thin delegate → `_thumbnails_mongo`."""
        return await _thumbnails.get_thumbnail(self, channel_id, telegram_msg_id, media_idx)

    async def delete_thumbnail(self, channel_id: int, telegram_msg_id: int, media_idx: int) -> None:
        """thin delegate → `_thumbnails_mongo`."""
        return await _thumbnails.delete_thumbnail(self, channel_id, telegram_msg_id, media_idx)

    async def list_thumbnails_for_message(
        self, channel_id: int, telegram_msg_id: int
    ) -> list[ThumbnailDTO]:
        """thin delegate → `_thumbnails_mongo`."""
        return await _thumbnails.list_thumbnails_for_message(self, channel_id, telegram_msg_id)

    async def count_thumbnails_by_object_key(self, object_key: str) -> int:
        """thin delegate → `_thumbnails_mongo`."""
        return await _thumbnails.count_thumbnails_by_object_key(self, object_key)


def _escape_regex(s: str) -> str:
    """转义 Mongo `$regex` 注入(2026-08-25 PR #3)。"""
    import re

    return re.escape(s)
