"""PostgreSQL 实现 — `asyncpg`。

- 显式 schema(内联 `SCHEMA_SQL`,2026-10-12 plan-batch3 目标 3 step 3.1
  从 `storage/schema.sql` 文件 inline;保留 `EXPECTED_SCHEMA` / `EXPECTED_TABLES`
  同源对照供 `introspect_schema()` 比对,两处统一改)
- JSON 列(`raw`):用 `json.dumps` / `jsonb` 类型
- 唯一约束 `(channel_id, telegram_msg_id)` 配合 ON CONFLICT 实现幂等 upsert
"""

from __future__ import annotations

import json
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
    ReactionDTO,
    SortDir,
    SortKey,
    ThumbnailDTO,
)
from tgmonitor.core.storage import _lifecycle_pg as _lifecycle
from tgmonitor.core.storage import _user_meta_pg as _user_meta
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.storage.schema_report import SchemaReport

log = logging.getLogger(__name__)

# 2026-10-16 plan-batch8-1.1:`SCHEMA_SQL` / `EXPECTED_SCHEMA` / `EXPECTED_TABLES`
# 与 6 个 lifecycle method 下沉到 `_lifecycle_pg.py`,facade 改 1 行 thin
# delegate。
# 2026-10-16 plan-batch8-1.2:5 个 user_meta method 下沉到 `_user_meta_pg.py`。
# 其它职责(频道 / 消息 / 媒体 / 缩略图)留本文件,后续 commit 1.3~1.6
# 逐步下沉。


# 2026-08-25 v1.3.0 PR #6:SortKey → SQL ORDER BY 列名映射。
# DATE 走 m.date;SIZE 走 me.file_size(可能 NULL → NULLS LAST 让 NULL 落到末尾);
# STATUS 走 me.download_status(枚举字符串字典序 = done<failed<pending<downloading)。
# value 是 (column, nulls_last) — direction 与 nulls_last 在拼 ORDER BY 时分别处理,
# 见 _media_order_by_clause(DESC 时变 `col DESC NULLS LAST`,ASC 时 `col ASC NULLS LAST`)。
_MEDIA_SORT_COLUMN: dict[SortKey, tuple[str, bool]] = {
    SortKey.DATE: ("m.date", False),
    SortKey.SIZE: ("me.file_size", True),
    SortKey.STATUS: ("me.download_status", False),
}


def _media_to_row(message_pk: int, m: MediaDTO, idx: int) -> tuple[Any, ...]:
    """media INSERT 占位;`idx` 当前未用(预留 future:把 idx 入表替代
    自动生成 media.id)— 保留参数为稳定性。
    """
    return (
        message_pk,
        m.type.value,
        m.mime_type,
        m.file_name,
        m.file_size,
        m.width,
        m.height,
        m.duration,
        m.telegram_file_id,
        # 2026-09-29:thumbnail_telegram_file_id 是新增列;MediaDownloader
        # 用它按需下载缩略图。media.thumb_key / thumb_backend 列旧 schema
        # 保留不写(由 schema.sql ALTER TABLE 占位;新代码一律走 thumbnails 表)。
        m.thumbnail_telegram_file_id,
        m.object_key,
        m.object_backend,
        m.emoji,
        m.download_status.value,
        m.download_error,
    )


def _row_to_channel(row: asyncpg.Record) -> ChannelDTO:
    return ChannelDTO(
        id=row["id"],
        title=row["title"],
        username=row["username"],
        kind=row["kind"],
        member_count=row["member_count"],
        created_at=row["created_at"],
        is_subscribed=bool(row.get("subscribed", True)),
        last_synced_at=row.get("last_synced_at"),
        # 2026-09-03 v1.6.0 PR #Q2:旧库可能没这列,get 兜底 None
        photo_local_key=row.get("photo_local_key"),
        # 2026-09-04 v1.6.4:spammer 过滤 UI 用;旧库 roundtrip 默认 False
        is_verified=bool(row.get("is_verified", False)),
        is_scam=bool(row.get("is_scam", False)),
        is_fake=bool(row.get("is_fake", False)),
        has_protected_content=bool(row.get("has_protected_content", False)),
    )


def _row_to_media(row: asyncpg.Record) -> MediaDTO:
    return MediaDTO(
        type=MediaType(row["type"]),
        mime_type=row["mime_type"],
        file_name=row["file_name"],
        file_size=row["file_size"],
        width=row["width"],
        height=row["height"],
        duration=row["duration"],
        telegram_file_id=row["telegram_file_id"],
        # 2026-09-29:缩略图改独立 thumbnails 表(MediaDTO 不再含
        # thumb_key / thumb_backend,旧 media 表这两列保留但不读不写)。
        thumbnail_telegram_file_id=row.get("thumbnail_telegram_file_id"),
        object_key=row["object_key"],
        object_backend=row["object_backend"],
        emoji=row["emoji"],
        download_status=_media_status(row.get("download_status")),
        download_error=row.get("download_error"),
    )


def _row_to_thumbnail(row: asyncpg.Record) -> ThumbnailDTO:
    return ThumbnailDTO(
        channel_id=row["channel_id"],
        telegram_msg_id=row["telegram_msg_id"],
        media_idx=row["media_idx"],
        object_key=row["object_key"],
        object_backend=row["object_backend"],
        mime_type=row["mime_type"],
        file_size=row["file_size"],
        width=row["width"],
        height=row["height"],
        sha256=row["sha256"],
        download_status=_media_status(row.get("download_status")),
        download_error=row.get("download_error"),
        telegram_thumb_file_id=row.get("telegram_thumb_file_id"),
    )


def _media_status(value: object | None) -> MediaDownloadStatus:
    """反序列化 download_status;非法 / 缺失(旧库)回退 pending。"""
    try:
        return MediaDownloadStatus(str(value))
    except ValueError:
        return MediaDownloadStatus.PENDING


def _row_to_message(row: asyncpg.Record, media: list[MediaDTO]) -> MessageDTO:
    raw = row["raw"]
    if isinstance(raw, str):
        raw = json.loads(raw)
    # 2026-08-27 v1.4.0 PR #10:reactions 是 JSONB list[dict];读时 None 表示从未
    # 推送(老库),[] 表示已清空。每条 dict 转 ReactionDTO。
    reactions_raw = row.get("reactions")
    reactions: list[ReactionDTO] | None
    if reactions_raw is None:
        reactions = None
    elif isinstance(reactions_raw, str):
        reactions = [ReactionDTO.from_dict(d) for d in json.loads(reactions_raw)]
    else:
        reactions = [ReactionDTO.from_dict(d) for d in reactions_raw]
    return MessageDTO(
        id=row["id"],
        channel_id=row["channel_id"],
        telegram_msg_id=row["telegram_msg_id"],
        author=row["author"],
        date=row["date"],
        text=row["text"] or "",
        views=row["views"],
        forwards=row["forwards"],
        reply_to_msg_id=row["reply_to_msg_id"],
        edited=row["edited"],
        media=media,
        raw=raw,
        # 2026-08-27 v1.4.0 PR #9:forward_origin 是 JSONB,asyncpg 已解;老库
        # ALTER TABLE 加的列,新库建表即有。其它 3 字段新列默认 False / NULL。
        forward_origin=json.loads(row["forward_origin"])
        if isinstance(row.get("forward_origin"), str)
        else row.get("forward_origin"),
        via_bot_user_id=row.get("via_bot_user_id"),
        media_album_id=row.get("media_album_id"),
        is_pinned=bool(row.get("is_pinned", False)),
        reactions=reactions,
        # 2026-09-09 v1.7.2:用户元数据 — 旧库 `.get` 兜底默认值
        is_favorite=bool(row.get("is_favorite", False)),
        tags=list(row.get("tags") or []),
        notes=row.get("notes") or "",
    )


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

    # ---- 频道 ----

    async def upsert_channel(self, channel: ChannelDTO) -> None:
        """全字段 upsert(含 subscribed) — 老调用方兼容;**新代码走 upsert_channel_metadata**。

        2026-09-03 v1.6.0 PR #Q2:加 `photo_local_key` 字段。
        2026-09-04 v1.6.4:加 4 个 spammer 过滤字段。
        """
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO channels
                    (id, title, username, kind, member_count, created_at,
                     subscribed, last_synced_at, photo_local_key,
                     is_verified, is_scam, is_fake, has_protected_content)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    username = EXCLUDED.username,
                    kind = EXCLUDED.kind,
                    member_count = EXCLUDED.member_count,
                    created_at = EXCLUDED.created_at,
                    subscribed = EXCLUDED.subscribed,
                    last_synced_at = EXCLUDED.last_synced_at,
                    photo_local_key = EXCLUDED.photo_local_key,
                    is_verified = EXCLUDED.is_verified,
                    is_scam = EXCLUDED.is_scam,
                    is_fake = EXCLUDED.is_fake,
                    has_protected_content = EXCLUDED.has_protected_content
                """,
                channel.id,
                channel.title,
                channel.username,
                channel.kind,
                channel.member_count,
                channel.created_at,
                channel.is_subscribed,
                channel.last_synced_at,
                channel.photo_local_key,
                channel.is_verified,
                channel.is_scam,
                channel.is_fake,
                channel.has_protected_content,
            )

    async def upsert_channel_metadata(self, channel: ChannelDTO) -> None:
        """只更元数据字段;subscribed 保持旧值(sync 用)。

        2026-09-03 v1.6.0 PR #Q2:加 `photo_local_key`。
        2026-09-04 v1.6.4:加 4 个 spammer 过滤字段。
        """
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO channels
                    (id, title, username, kind, member_count, created_at,
                     subscribed, last_synced_at, photo_local_key,
                     is_verified, is_scam, is_fake, has_protected_content)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    username = EXCLUDED.username,
                    kind = EXCLUDED.kind,
                    member_count = EXCLUDED.member_count,
                    created_at = EXCLUDED.created_at,
                    last_synced_at = EXCLUDED.last_synced_at,
                    photo_local_key = EXCLUDED.photo_local_key,
                    is_verified = EXCLUDED.is_verified,
                    is_scam = EXCLUDED.is_scam,
                    is_fake = EXCLUDED.is_fake,
                    has_protected_content = EXCLUDED.has_protected_content
                """,
                channel.id,
                channel.title,
                channel.username,
                channel.kind,
                channel.member_count,
                channel.created_at,
                # 首次插入时给个合理默认(False),后续 DO UPDATE 不动 subscribed
                channel.is_subscribed,
                channel.last_synced_at,
                channel.photo_local_key,
                channel.is_verified,
                channel.is_scam,
                channel.is_fake,
                channel.has_protected_content,
            )

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
        """2026-08-27 v1.4.0 PR #14:Postgres 部分更新 — `COALESCE($N, 列名)`
        把 None 映射成「保留旧值」,只动传入字段。

        2026-09-03 v1.6.0 PR #Q2:加 `photo_local_key` 字段。
        2026-09-04 v1.6.4:加 4 个 spammer 过滤字段。
        """
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE channels SET
                    title                = COALESCE($1, title),
                    username             = COALESCE($2, username),
                    member_count         = COALESCE($3, member_count),
                    photo_local_key      = COALESCE($4, photo_local_key),
                    is_verified          = COALESCE($5, is_verified),
                    is_scam              = COALESCE($6, is_scam),
                    is_fake              = COALESCE($7, is_fake),
                    has_protected_content = COALESCE($8, has_protected_content)
                WHERE id = $9
                """,
                title,
                username,
                member_count,
                photo_local_key,
                is_verified,
                is_scam,
                is_fake,
                has_protected_content,
                channel_id,
            )

    async def set_channel_subscribed(self, channel_id: int, subscribed: bool) -> None:
        """只设订阅标志;频道未建档时用 id 做个 stub(后续会被 metadata 覆盖)。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            # 不存在就先建一条 stub
            await conn.execute(
                """
                INSERT INTO channels (id, title, subscribed)
                VALUES ($1, $2, $3)
                ON CONFLICT (id) DO UPDATE SET subscribed = EXCLUDED.subscribed
                """,
                channel_id,
                f"#{channel_id}",
                subscribed,
            )

    async def list_channels(self) -> list[ChannelDTO]:
        """所有频道(按 id 升序);含未订阅的。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title, username, kind, member_count, created_at, "
                "subscribed, last_synced_at, photo_local_key, "
                "is_verified, is_scam, is_fake, has_protected_content "
                "FROM channels ORDER BY id"
            )
        return [_row_to_channel(r) for r in rows]

    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        """只返 subscribed=TRUE 的频道(按 id 升序);供 MonitorService 喂白名单。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title, username, kind, member_count, created_at, "
                "subscribed, last_synced_at, photo_local_key, "
                "is_verified, is_scam, is_fake, has_protected_content "
                "FROM channels WHERE subscribed = TRUE ORDER BY id"
            )
        return [_row_to_channel(r) for r in rows]

    async def get_channel(self, channel_id: int) -> ChannelDTO | None:
        """单频道;不存在返 None。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, title, username, kind, member_count, created_at, "
                "subscribed, last_synced_at, photo_local_key, "
                "is_verified, is_scam, is_fake, has_protected_content "
                "FROM channels WHERE id = $1",
                channel_id,
            )
        return _row_to_channel(row) if row else None

    async def delete_channel(self, channel_id: int) -> None:
        """删频道;ON DELETE CASCADE 自动带 messages / media(由 schema 定义)。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM channels WHERE id = $1", channel_id)

    async def get_max_telegram_msg_id(self, channel_id: int) -> int | None:
        """续拉历史用 — 该频道已落库的最大 telegram_msg_id;无历史返 None。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT MAX(telegram_msg_id) FROM messages WHERE channel_id = $1",
                channel_id,
            )

    async def get_meta(self, key: str) -> str | None:
        """全局单值元数据;不存在返 None。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT value FROM meta WHERE key = $1",
                key,
            )

    async def set_meta(self, key: str, value: str) -> None:
        """upsert 语义:ON CONFLICT DO UPDATE 覆盖。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO meta (key, value) VALUES ($1, $2)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                key,
                value,
            )

    # ---- 消息 ----

    async def save_message(self, message: MessageDTO) -> int:
        """幂等 upsert:返回 messages.id。

        2026-08-31 v1.5.0 PR #A7 修 latent prod FK violation:`messages.channel_id`
        外键引用 `channels.id`,但 monitor 路径可能先收到消息、再调
        `sync_channels`(频道元数据从 TDLib 拉到 storage 有时延)— 若调用方
        未显式 `upsert_channel`,PG 会抛 `ForeignKeyViolationError`。
        InMemoryRepository 都隐式建频道
        (id + title="#<id>"),本实现对齐:INSERT 前 `ON CONFLICT DO NOTHING`
        保底,绝不覆盖调用方已 upsert 的完整 metadata。
        """
        assert self._pool is not None
        raw_json = json.dumps(message.raw) if message.raw is not None else None
        # 2026-08-27 v1.4.0 PR #9:forward_origin 序列化为 JSONB;
        # asyncpg 自动解 dict,所以读出来不需要二次 json.loads。
        forward_origin_json = (
            json.dumps(message.forward_origin) if message.forward_origin is not None else None
        )
        # 2026-08-27 v1.4.0 PR #10:reactions 序列化为 JSONB list[dict]。
        reactions_json = (
            json.dumps([r.to_dict() for r in message.reactions])
            if message.reactions is not None
            else None
        )
        async with self._pool.acquire() as conn, conn.transaction():
            # 隐式频道占位 — 与 InMemoryRepository 对齐。
            # 用最小字段占位(`title=#<id>`),已有频道(完整 metadata)不动。
            # 显式 subscribed=FALSE 而非依赖列默认值:旧库 `ALTER TABLE ... DEFAULT
            # TRUE` 迁移可能让 placeholder 错标已订阅,与 InMemory 语义
            # (ChannelDTO.is_subscribed=False) 不一致。
            await conn.execute(
                """
                INSERT INTO channels (id, title, subscribed)
                VALUES ($1, $2, FALSE)
                ON CONFLICT (id) DO NOTHING
                """,
                message.channel_id,
                f"#{message.channel_id}",
            )
            row = await conn.fetchrow(
                """
                    INSERT INTO messages
                        (channel_id, telegram_msg_id, author, date, text,
                         views, forwards, reply_to_msg_id, edited, raw,
                         forward_origin, via_bot_user_id, media_album_id, is_pinned,
                         reactions,
                         is_favorite, tags, notes)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb,
                            $11::jsonb, $12, $13, $14,
                            $15::jsonb,
                            $16, $17::text[], $18)
                    ON CONFLICT (channel_id, telegram_msg_id) DO UPDATE SET
                        author = EXCLUDED.author,
                        date = EXCLUDED.date,
                        text = EXCLUDED.text,
                        views = EXCLUDED.views,
                        forwards = EXCLUDED.forwards,
                        reply_to_msg_id = EXCLUDED.reply_to_msg_id,
                        edited = EXCLUDED.edited,
                        raw = EXCLUDED.raw,
                        forward_origin = EXCLUDED.forward_origin,
                        via_bot_user_id = EXCLUDED.via_bot_user_id,
                        media_album_id = EXCLUDED.media_album_id,
                        is_pinned = EXCLUDED.is_pinned,
                        reactions = EXCLUDED.reactions,
                        is_favorite = EXCLUDED.is_favorite,
                        tags = EXCLUDED.tags,
                        notes = EXCLUDED.notes
                    RETURNING id
                    """,
                message.channel_id,
                message.telegram_msg_id,
                message.author,
                message.date,
                message.text,
                message.views,
                message.forwards,
                message.reply_to_msg_id,
                message.edited,
                raw_json,
                forward_origin_json,
                message.via_bot_user_id,
                message.media_album_id,
                message.is_pinned,
                reactions_json,
                message.is_favorite,
                message.tags,
                message.notes,
            )
            msg_pk = row["id"]
            # 媒体:先清后插(简化语义;真实场景可改为按 stable id 合并)
            await conn.execute("DELETE FROM media WHERE message_id = $1", msg_pk)
            for idx, m in enumerate(message.media):
                await conn.execute(
                    """
                        INSERT INTO media
                            (message_id, type, mime_type, file_name, file_size,
                             width, height, duration, telegram_file_id,
                             thumbnail_telegram_file_id,
                             object_key, object_backend,
                             emoji, download_status, download_error)
                        VALUES
                            ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
                             $11,$12,$13,$14,$15)
                        """,
                    *_media_to_row(msg_pk, m, idx),
                )
        message.id = msg_pk
        return msg_pk

    async def update_message(self, message: MessageDTO) -> None:
        """代理到 save_message(upsert 语义一致)。"""
        await self.save_message(message)  # upsert 语义一致

    async def update_message_interactions(
        self,
        channel_id: int,
        telegram_msg_id: int,
        *,
        views: int | None = None,
        reactions: list[Any] | None = None,
    ) -> None:
        """2026-08-27 v1.4.0 PR #10:Postgres 走单条 UPDATE + COALESCE 增量更新。

        views / reactions 各自有 nullable 语义:
        - views=None   → 不动
        - reactions=None → 不动
        - reactions=[] → 清空(JSONB `[]`)

        不存在消息 idempotent 不抛(0 rows updated 是合法的)。
        """
        assert self._pool is not None
        # 序列化 reactions → list[dict] → JSONB;None 跳过,[] 写空数组
        if reactions is None:
            reactions_arg: Any = None
        else:
            reactions_arg = json.dumps(
                [r.to_dict() if isinstance(r, ReactionDTO) else r for r in reactions]
            )
        if views is None:
            views_arg: Any = None
        else:
            views_arg = views

        # 2026-08-31 v1.5.0 PR #A7 hotfix #3:用位置计数器构 SET 子句 —
        # 旧实现 views_clause / reactions_clause 硬编 $4 / $5,只在
        # 「views + reactions 同时给」时对齐;只给 views(reactions=None)
        # 时 args=[42, 100, 1] 但 SQL 写 $4,asyncpg 抛
        # IndeterminateDatatypeError。改成按当前 args 长度算 $N。
        set_parts: list[str] = []
        args: list[Any] = []
        if views_arg is not None:
            args.append(views_arg)
            set_parts.append(f"views = ${len(args)}")
        if reactions_arg is not None:
            args.append(reactions_arg)
            set_parts.append(f"reactions = ${len(args)}::jsonb")
        if not set_parts:
            return  # 两边都 None → no-op
        set_sql = ", ".join(set_parts)
        args.append(channel_id)
        channel_arg = len(args)
        args.append(telegram_msg_id)
        msg_arg = len(args)
        sql = f"""
            UPDATE messages SET {set_sql}
            WHERE channel_id = ${channel_arg}
              AND telegram_msg_id = ${msg_arg}
        """
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *args)

    async def update_message_pin(
        self,
        channel_id: int,
        telegram_msg_id: int,
        is_pinned: bool,
    ) -> None:
        """2026-09-10 v1.7.3:PG 单条 UPDATE 设 is_pinned。

        `is_pinned` 列(v1.5.0 起)在 `schema.sql` 已 `NOT NULL DEFAULT FALSE`,
        走标准 UPDATE 即可;不存在消息 0 matched 不抛(idempotent)。
        """
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET is_pinned = $3 WHERE channel_id = $1 AND telegram_msg_id = $2",
                channel_id,
                telegram_msg_id,
                is_pinned,
            )

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """删单条消息;media 行通过 FK CASCADE 自动删。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM messages WHERE channel_id = $1 AND telegram_msg_id = $2",
                channel_id,
                telegram_msg_id,
            )

    async def delete_messages(self, channel_id: int, msg_ids: list[int]) -> None:
        """2026-09-08 v1.7.0:批量删单频道 N 条消息;media 行 FK CASCADE 自动删。

        单发 SQL:`DELETE WHERE channel_id=$1 AND telegram_msg_id = ANY($2)`
        — 比 N 次 delete_message 节省 N-1 个 round-trip。
        """
        if not msg_ids:
            return
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM messages WHERE channel_id = $1 AND telegram_msg_id = ANY($2::bigint[])",
                channel_id,
                msg_ids,
            )

    async def get_message(self, channel_id: int, telegram_msg_id: int) -> MessageDTO | None:
        """单条消息 + 关联 media;不存在返 None。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM messages WHERE channel_id = $1 AND telegram_msg_id = $2",
                channel_id,
                telegram_msg_id,
            )
            if not row:
                return None
            media_rows = await conn.fetch(
                "SELECT * FROM media WHERE message_id = $1 ORDER BY id", row["id"]
            )
        return _row_to_message(row, [_row_to_media(m) for m in media_rows])

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
        """按时间升序 + id 升序(与 Mongo 对齐);media 二次查询拼回。

        `channel_ids` 走 ANY($1::bigint[]);`limit` = 最近 N 条(倒序取前 N
        后在内存反转为升序,LIMIT 在 SQL 层)。
        `offset` (v1.4.0 PR #12):从尾部跳过 offset 条再取 limit — 例
        limit=2 offset=2 倒数 [3,4] 条。SQL 用 `LIMIT $limit OFFSET $offset`
        配合 date DESC,再 reverse 转升序。

        `search` (v1.5.1 PR #B2):`LOWER(text) LIKE` OR `EXISTS media.file_name LIKE`,
        通配符 `\\` / `%` / `_` 必须 escape,否则用户输入 `%` 匹配一切。

        2026-09-14 v1.7.5 PR #8:`favorite_only` / `tag_only` / `pinned_only`
        走 SQL `WHERE` 子句(`is_favorite` / `cardinality(tags) > 0` /
        `is_pinned`);AND 语义任一 True 必须命中。
        """
        assert self._pool is not None
        if not channel_ids:
            return []
        where: list[str] = ["channel_id = ANY($1::bigint[])"]
        params: list[Any] = [channel_ids]
        if date_from is not None:
            params.append(date_from)
            where.append(f"date >= ${len(params)}")
        if date_to is not None:
            params.append(date_to)
            where.append(f"date <= ${len(params)}")
        if favorite_only:
            where.append("is_favorite = TRUE")
        if tag_only:
            # 2026-09-14 v1.7.5 PR #8:tags TEXT[] 非空判定 — cardinality() O(1)。
            where.append("cardinality(tags) > 0")
        if pinned_only:
            where.append("is_pinned = TRUE")
        if search:
            # 转义 LIKE 通配符:`%` / `_` / `\`(用户输入的 `\` 也需转义为 `\\`)。
            escaped = search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            # `ESCAPE '\'` 告诉 SQL 把 `\` 当作自定义 escape 字符;
            # 子句顺序:先验 text,再验 media 子查询。
            where.append(
                f"(LOWER(text) LIKE ${len(params) + 1} ESCAPE '\\' "
                # 2026-09-04 v1.6.3 patch:子查询里用 messages 全表名,外层
                # `SELECT * FROM messages` 没给别名 `m`,原 `m.id` 在
                # asyncpg 真 PG 上 UndefinedTableError(本地集成测试用
                # InMemoryRepository 测不到这条路径,只有 CI testcontainers
                # 真 PG 才暴露)。
                f"OR EXISTS (SELECT 1 FROM media WHERE media.message_id=messages.id "
                f"AND LOWER(media.file_name) LIKE ${len(params) + 2} ESCAPE '\\'))"
            )
            params.append(like)
            params.append(like)
        # `limit` = 最近 N 条:倒序取前 N 再反转为升序(与 InMemory / Mongo 语义一致)。
        order_by = "date DESC, id DESC" if limit is not None else "date ASC, id ASC"
        sql = "SELECT * FROM messages WHERE " + " AND ".join(where) + f" ORDER BY {order_by}"
        if limit is not None:
            params.append(limit)
            sql += f" LIMIT ${len(params)}"
            if offset > 0:
                # 2026-08-27 v1.4.0 PR #12:从尾部跳过 offset 再取 limit。
                # 大 offset 下 OFFSET 是 O(N),docstring 写明建议收窄 date 过滤。
                params.append(offset)
                sql += f" OFFSET ${len(params)}"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            if limit is not None:
                rows = list(reversed(rows))
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            media_rows = await conn.fetch(
                "SELECT * FROM media WHERE message_id = ANY($1::bigint[]) ORDER BY id",
                ids,
            )
        by_msg: dict[int, list[MediaDTO]] = {}
        for mr in media_rows:
            by_msg.setdefault(mr["message_id"], []).append(_row_to_media(mr))
        return [_row_to_message(r, by_msg.get(r["id"], [])) for r in rows]

    async def count_messages(self, channel_id: int) -> int:
        """该频道已落库消息数;走 COUNT(*) 聚合,不应用 date 过滤。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*)::int FROM messages WHERE channel_id = $1", channel_id
            )

    async def aggregate_per_channel(self, channel_ids: list[int]) -> dict[int, ChannelStats]:
        """2026-08-27 v1.4.0 PR #15:Postgres 单 GROUP BY 查询,一次性拿所有
        channel 的 messages / media / done_media / last_date。

        SQL plan:`messages LEFT JOIN media ON me.message_id = m.id` + `GROUP BY
        channel_id` + `FILTER (download_status = 'done')`。PostgreSQL 9.4+ 语法。

        缺失 channel_id(没消息)在返回 dict 里**不**存在 — 调用方按需 default。
        """
        if not channel_ids:
            return {}
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT m.channel_id,
                       count(DISTINCT m.id) AS messages,
                       count(me.id) AS media,
                       count(me.id) FILTER (
                           WHERE me.download_status = 'done'
                       ) AS done_media,
                       max(m.date) AS last_date
                FROM messages m
                LEFT JOIN media me ON me.message_id = m.id
                WHERE m.channel_id = ANY($1::bigint[])
                GROUP BY m.channel_id
                """,
                channel_ids,
            )
        out: dict[int, ChannelStats] = {}
        for r in rows:
            out[int(r["channel_id"])] = ChannelStats(
                messages=int(r["messages"]),
                media=int(r["media"]),
                done_media=int(r["done_media"]),
                last_date=r["last_date"],
            )
        return out

    async def find_media_by_file_id(self, telegram_file_id: str) -> MediaDTO | None:
        """跨频道去重:任一已 DONE 的同 file_id media → 返 DTO。

        命中条件 `object_key IS NOT NULL AND download_status = 'done'`;partial
        index(`idx_media_telegram_file_id`)保证单条查询 O(log N) → 命中 LIMIT 1。
        """
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM media
                WHERE telegram_file_id = $1
                  AND object_key IS NOT NULL
                  AND download_status = 'done'
                ORDER BY id DESC
                LIMIT 1
                """,
                telegram_file_id,
            )
        if row is None:
            return None
        return _row_to_media(row)

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
        """2026-08-25 PR #3:Postgres 后端的 list_media — `messages m JOIN media` + filter。

        SQL `JOIN` 在 PG 里走 `idx_media_*` 索引;`ORDER BY <sort> <dir>, m.id DESC`
        保证结果顺序稳定(2026-08-25 v1.3.0 PR #6:`sort`/`sort_dir` 可切)。
        `search` 用 `LOWER(file_name) LIKE LOWER('%...%')` 简单 substring
        match(MVP,后续用 pg_trgm 优化)。
        """
        assert self._pool is not None
        where_sql, params, next_idx = self._media_where_clause(
            channel_ids,
            status,
            media_type,
            search,
        )
        sort_col, nulls_last = _MEDIA_SORT_COLUMN[sort]
        nulls_clause = " NULLS LAST" if nulls_last else ""
        # 2026-09-25 fix(media-bug):`media_idx` 不能 SELECT me.media_idx —
        # `media` 表没有该列(Mongo 用 `$unwind includeArrayIndex`、
        # InMemory 用 enumerate,都运行时生成;PG 的 PR #3 把 media 拆成
        # 独立表时漏了列)。改成窗口函数:`ROW_NUMBER() OVER (PARTITION BY
        # m.id ORDER BY me.id) - 1` 按 me.id(BIGSERIAL,等价于 save_message
        # 的 INSERT 顺序)在每条 message 内部从 0 起编。与 InMemory enumerate
        # / Mongo unwind 顺序对齐,UI `(channel_id, telegram_msg_id,
        # media_idx)` 三元组定位语义不变。
        sql = [
            "SELECT m.*, me.id AS media_id, me.type AS media_type,",
            "       me.mime_type, me.file_name, me.file_size, me.width, me.height,",
            "       me.duration, me.telegram_file_id, me.object_key,",
            # 2026-09-29:thumb_key / thumb_backend 已从 MediaDTO 删除,
            # list_media 不再 SELECT 它们(列保留在 schema 但不读);改 SELECT
            # thumbnail_telegram_file_id 让 MediaDTO 拿到该字段。
            "       me.object_backend, me.thumbnail_telegram_file_id, me.emoji,",
            "       me.download_status AS media_dl_status, me.download_error,",
            "       ROW_NUMBER() OVER (PARTITION BY m.id ORDER BY me.id) - 1 AS media_idx",
            "FROM messages m JOIN media me ON me.message_id = m.id",
            f"WHERE {where_sql}",
            f"ORDER BY {sort_col} {sort_dir.value.upper()}{nulls_clause}, m.id DESC, media_idx ASC",
        ]
        if limit:
            sql.append(f"LIMIT ${next_idx} OFFSET ${next_idx + 1}")
            params.extend([limit, offset])
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("\n".join(sql), *params)

        # row → media dict(2 处复用,抽出 helper 避免重复)
        def _row_to_media_dict(r: asyncpg.Record) -> dict[str, Any]:
            return {
                "type": r["media_type"],
                "mime_type": r["mime_type"],
                "file_name": r["file_name"],
                "file_size": r["file_size"],
                "width": r["width"],
                "height": r["height"],
                "duration": r["duration"],
                "telegram_file_id": r["telegram_file_id"],
                "object_key": r["object_key"],
                "object_backend": r["object_backend"],
                "emoji": r["emoji"],
                "download_status": r["media_dl_status"],
                "download_error": r["download_error"],
                # 2026-09-29:thumb_key / thumb_backend 已删,改 thumbnail_telegram_file_id
                "thumbnail_telegram_file_id": r["thumbnail_telegram_file_id"],
            }

        msg_keys = (
            "id",
            "channel_id",
            "telegram_msg_id",
            "author",
            "date",
            "text",
            "views",
            "forwards",
            "reply_to_msg_id",
            "edited",
            "raw",
        )
        return [
            (
                _row_to_message(
                    {k: r[k] for k in msg_keys}, [_row_to_media(_row_to_media_dict(r))]
                ),
                r["media_idx"],
                _row_to_media(_row_to_media_dict(r)),
            )
            for r in rows
        ]

    async def count_media(
        self,
        *,
        channel_ids: list[int] | None = None,
        status: MediaDownloadStatus | None = None,
        media_type: MediaType | None = None,
        search: str = "",
    ) -> int:
        """2026-08-25 v1.3.0 PR #6:与 list_media 同 filter,数总数供分页 UI 用。

        复用 `_media_where_clause` 拼同 WHERE 子句,主查询去掉 ORDER/LIMIT/OFFSET,
        `SELECT count(*)::int`。Postgres 走 `JOIN + WHERE` 同索引,O(log N)。
        """
        assert self._pool is not None
        where_sql, params, _ = self._media_where_clause(
            channel_ids,
            status,
            media_type,
            search,
        )
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                f"SELECT count(*)::int FROM messages m "
                f"JOIN media me ON me.message_id = m.id WHERE {where_sql}",
                *params,
            )

    def _media_where_clause(
        self,
        channel_ids: list[int] | None,
        status: MediaDownloadStatus | None,
        media_type: MediaType | None,
        search: str,
    ) -> tuple[str, list[Any], int]:
        """2026-08-25 v1.3.0 PR #6:Postgres `list_media` / `count_media` 共用的 WHERE 拼装。

        返回 `(where_sql, params, next_param_idx)` — caller 接到后可继续
        追加 `ORDER BY` / `LIMIT` / `OFFSET`(注意 next_idx 用)。`WHERE 1=1`
        占位保留,后续 AND 条件直接拼字符串。
        """
        clauses: list[str] = ["1=1"]
        params: list[Any] = []
        idx = 1
        if channel_ids:
            clauses.append(f"m.channel_id = ANY(${idx})")
            params.append(channel_ids)
            idx += 1
        if status is not None:
            clauses.append(f"me.download_status = ${idx}")
            params.append(status.value)
            idx += 1
        if media_type is not None:
            clauses.append(f"me.type = ${idx}")
            params.append(media_type.value)
            idx += 1
        if search:
            clauses.append(f"LOWER(COALESCE(me.file_name, '')) LIKE ${idx}")
            params.append(f"%{search.lower()}%")
            idx += 1
        return " AND ".join(clauses), params, idx

    async def count_media_by_object_key(self, object_key: str) -> int:
        """2026-08-25 PR #3:refcount — `SELECT count(*)` 走索引 O(log N)。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*)::int FROM media WHERE object_key = $1",
                object_key,
            )

    async def count_media_by_channel(self, channel_id: int) -> int:
        """2026-08-25 v1.3.0 PR #8:该频道全部 media 数(含 PENDING/FAILED)。

        `JOIN messages` + WHERE channel_id + count media rows。
        """
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*)::int FROM media me "
                "JOIN messages m ON me.message_id = m.id WHERE m.channel_id = $1",
                channel_id,
            )

    # ---- 缩略图(2026-09-29 独立存储) ----

    async def save_thumbnail(self, thumb: ThumbnailDTO) -> None:
        """upsert thumb 行;主键 (channel_id, telegram_msg_id, media_idx)。

        ON CONFLICT 三 key 时覆盖所有可写列;幂等 — 多次调以最新一次为准。
        与 media 行无关(各自独立的 storage 状态机)。
        """
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                    INSERT INTO thumbnails
                        (channel_id, telegram_msg_id, media_idx,
                         object_key, object_backend,
                         mime_type, file_size, width, height, sha256,
                         download_status, download_error,
                         telegram_thumb_file_id)
                    VALUES
                        ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                    ON CONFLICT (channel_id, telegram_msg_id, media_idx) DO UPDATE SET
                        object_key = EXCLUDED.object_key,
                        object_backend = EXCLUDED.object_backend,
                        mime_type = EXCLUDED.mime_type,
                        file_size = EXCLUDED.file_size,
                        width = EXCLUDED.width,
                        height = EXCLUDED.height,
                        sha256 = EXCLUDED.sha256,
                        download_status = EXCLUDED.download_status,
                        download_error = EXCLUDED.download_error,
                        telegram_thumb_file_id = EXCLUDED.telegram_thumb_file_id
                    """,
                thumb.channel_id,
                thumb.telegram_msg_id,
                thumb.media_idx,
                thumb.object_key,
                thumb.object_backend,
                thumb.mime_type,
                thumb.file_size,
                thumb.width,
                thumb.height,
                thumb.sha256,
                thumb.download_status.value,
                thumb.download_error,
                thumb.telegram_thumb_file_id,
            )

    async def get_thumbnail(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> ThumbnailDTO | None:
        """单条 thumb;不存在返 None(不抛)。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                    SELECT channel_id, telegram_msg_id, media_idx,
                           object_key, object_backend,
                           mime_type, file_size, width, height, sha256,
                           download_status, download_error,
                           telegram_thumb_file_id
                    FROM thumbnails
                    WHERE channel_id = $1 AND telegram_msg_id = $2
                          AND media_idx = $3
                    """,
                channel_id,
                telegram_msg_id,
                media_idx,
            )
            return _row_to_thumbnail(row) if row else None

    async def delete_thumbnail(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        """删 thumb 行;不存在不抛(idempotent)。

        注:`DELETE FROM messages` 时 FK CASCADE 自动调本方法,
        无需手工再调(走 schema 的 ON DELETE CASCADE)。
        """
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM thumbnails "
                "WHERE channel_id = $1 AND telegram_msg_id = $2 AND media_idx = $3",
                channel_id,
                telegram_msg_id,
                media_idx,
            )

    async def list_thumbnails_for_message(
        self,
        channel_id: int,
        telegram_msg_id: int,
    ) -> list[ThumbnailDTO]:
        """一条消息的全部 thumb(通常 0 或 1 条)。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                    SELECT channel_id, telegram_msg_id, media_idx,
                           object_key, object_backend,
                           mime_type, file_size, width, height, sha256,
                           download_status, download_error,
                           telegram_thumb_file_id
                    FROM thumbnails
                    WHERE channel_id = $1 AND telegram_msg_id = $2
                    ORDER BY media_idx
                    """,
                channel_id,
                telegram_msg_id,
            )
            return [_row_to_thumbnail(r) for r in rows]

    async def count_thumbnails_by_object_key(self, object_key: str) -> int:
        """refcount — `SELECT count(*)` 走 idx_thumbnails_object_key 索引 O(log N)。

        `reconcile_orphans` 用:扫 thumb/ prefix 的 ObjectStore key 后,
        计数 >0 = 被 storage 引用,不算孤儿。
        """
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*)::int FROM thumbnails WHERE object_key = $1",
                object_key,
            )
