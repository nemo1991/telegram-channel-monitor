"""PostgreSQL 实现 — `asyncpg`。

- 显式 schema(schema.sql 启动时执行)
- JSON 列(`raw`):用 `json.dumps` / `jsonb` 类型
- 唯一约束 `(channel_id, telegram_msg_id)` 配合 ON CONFLICT 实现幂等 upsert
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg

from tgmonitor.core.dto import ChannelDTO, MediaDTO, MediaType, MessageDTO
from tgmonitor.core.storage.repository import StorageRepository

SCHEMA_FILE = Path(__file__).parent / "schema.sql"


def _media_to_row(message_pk: int, m: MediaDTO, idx: int) -> tuple[Any, ...]:
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
        m.object_key,
        m.object_backend,
        m.thumb_key,
        m.thumb_backend,
        m.emoji,
    )


def _row_to_channel(row: asyncpg.Record) -> ChannelDTO:
    return ChannelDTO(
        id=row["id"],
        title=row["title"],
        username=row["username"],
        kind=row["kind"],
        member_count=row["member_count"],
        created_at=row["created_at"],
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
        object_key=row["object_key"],
        object_backend=row["object_backend"],
        thumb_key=row["thumb_key"],
        thumb_backend=row["thumb_backend"],
        emoji=row["emoji"],
    )


def _row_to_message(row: asyncpg.Record, media: list[MediaDTO]) -> MessageDTO:
    raw = row["raw"]
    if isinstance(raw, str):
        raw = json.loads(raw)
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
    )


class PostgresRepository(StorageRepository):
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    # ---- 生命周期 ----

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def init_schema(self) -> None:
        assert self._pool is not None
        sql = SCHEMA_FILE.read_text(encoding="utf-8")
        async with self._pool.acquire() as conn:
            await conn.execute(sql)

    async def ping(self) -> bool:
        assert self._pool is not None
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    # ---- 频道 ----

    async def upsert_channel(self, channel: ChannelDTO) -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO channels (id, title, username, kind, member_count, created_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    username = EXCLUDED.username,
                    kind = EXCLUDED.kind,
                    member_count = EXCLUDED.member_count
                """,
                channel.id,
                channel.title,
                channel.username,
                channel.kind,
                channel.member_count,
                channel.created_at,
            )

    async def list_channels(self) -> list[ChannelDTO]:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title, username, kind, member_count, created_at "
                "FROM channels ORDER BY id"
            )
        return [_row_to_channel(r) for r in rows]

    async def get_channel(self, channel_id: int) -> ChannelDTO | None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, title, username, kind, member_count, created_at "
                "FROM channels WHERE id = $1",
                channel_id,
            )
        return _row_to_channel(row) if row else None

    async def delete_channel(self, channel_id: int) -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM channels WHERE id = $1", channel_id)

    # ---- 消息 ----

    async def save_message(self, message: MessageDTO) -> int:
        """幂等 upsert:返回 messages.id。"""
        assert self._pool is not None
        raw_json = json.dumps(message.raw) if message.raw is not None else None
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                    INSERT INTO messages
                        (channel_id, telegram_msg_id, author, date, text,
                         views, forwards, reply_to_msg_id, edited, raw)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
                    ON CONFLICT (channel_id, telegram_msg_id) DO UPDATE SET
                        author = EXCLUDED.author,
                        date = EXCLUDED.date,
                        text = EXCLUDED.text,
                        views = EXCLUDED.views,
                        forwards = EXCLUDED.forwards,
                        reply_to_msg_id = EXCLUDED.reply_to_msg_id,
                        edited = EXCLUDED.edited,
                        raw = EXCLUDED.raw
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
                             object_key, object_backend, thumb_key, thumb_backend,
                             emoji)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                        """,
                    *_media_to_row(msg_pk, m, idx),
                )
        message.id = msg_pk
        return msg_pk

    async def update_message(self, message: MessageDTO) -> None:
        await self.save_message(message)  # upsert 语义一致

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM messages WHERE channel_id = $1 AND telegram_msg_id = $2",
                channel_id,
                telegram_msg_id,
            )

    async def get_message(
        self, channel_id: int, telegram_msg_id: int
    ) -> MessageDTO | None:
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
    ) -> list[MessageDTO]:
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
        sql = (
            "SELECT * FROM messages WHERE "
            + " AND ".join(where)
            + " ORDER BY date ASC, id ASC"
        )
        if limit is not None:
            params.append(limit)
            sql += f" LIMIT ${len(params)}"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
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
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*)::int FROM messages WHERE channel_id = $1", channel_id
            )
