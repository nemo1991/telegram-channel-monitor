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

from tgmonitor.core.dto import (
    ChannelDTO,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    SortDir,
    SortKey,
)
from tgmonitor.core.storage.repository import StorageRepository

SCHEMA_FILE = Path(__file__).parent / "schema.sql"

# 2026-08-25 v1.3.0 PR #6:SortKey → SQL ORDER BY 列名映射。
# DATE 走 m.date;SIZE 走 me.file_size(可能 NULL → NULLS LAST 让 NULL 落到末尾);
# STATUS 走 me.download_status(枚举字符串字典序 = done<failed<pending<downloading)。
_MEDIA_SORT_COLUMN: dict[SortKey, str] = {
    SortKey.DATE: "m.date",
    SortKey.SIZE: "me.file_size NULLS LAST",
    SortKey.STATUS: "me.download_status",
}


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
        download_status=_media_status(row.get("download_status")),
        download_error=row.get("download_error"),
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
    """PostgreSQL 实现 — `asyncpg` 连接池 + ON CONFLICT 幂等 upsert。

    media 是独立表 + FK CASCADE;ON CONFLICT 配合 (channel_id, telegram_msg_id)
    唯一约束实现 save_message 幂等。
    """

    def __init__(self, dsn: str) -> None:
        """`dsn` = asyncpg DSN;实际 pool 在 connect() 时建。"""
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    # ---- 生命周期 ----

    async def connect(self) -> None:
        """建 asyncpg 连接池(1-10 个连接);后续 query 都从池里取。"""
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10)

    async def close(self) -> None:
        """关连接池;幂等(None 时 no-op)。"""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def init_schema(self) -> None:
        """执行 schema.sql(创建表 + 索引);幂等(全 IF NOT EXISTS)。"""
        assert self._pool is not None
        sql = SCHEMA_FILE.read_text(encoding="utf-8")
        async with self._pool.acquire() as conn:
            await conn.execute(sql)

    async def ping(self) -> bool:
        """SELECT 1 探活;任何异常返 False。"""
        assert self._pool is not None
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    # ---- 频道 ----

    async def upsert_channel(self, channel: ChannelDTO) -> None:
        """全字段 upsert(含 subscribed) — 老调用方兼容;**新代码走 upsert_channel_metadata**。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO channels
                    (id, title, username, kind, member_count, created_at,
                     subscribed, last_synced_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    username = EXCLUDED.username,
                    kind = EXCLUDED.kind,
                    member_count = EXCLUDED.member_count,
                    created_at = EXCLUDED.created_at,
                    subscribed = EXCLUDED.subscribed,
                    last_synced_at = EXCLUDED.last_synced_at
                """,
                channel.id,
                channel.title,
                channel.username,
                channel.kind,
                channel.member_count,
                channel.created_at,
                channel.is_subscribed,
                channel.last_synced_at,
            )

    async def upsert_channel_metadata(self, channel: ChannelDTO) -> None:
        """只更元数据字段;subscribed 保持旧值(sync 用)。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO channels
                    (id, title, username, kind, member_count, created_at,
                     subscribed, last_synced_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    username = EXCLUDED.username,
                    kind = EXCLUDED.kind,
                    member_count = EXCLUDED.member_count,
                    created_at = EXCLUDED.created_at,
                    last_synced_at = EXCLUDED.last_synced_at
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
            )

    async def set_channel_subscribed(
        self, channel_id: int, subscribed: bool
    ) -> None:
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
                channel_id, f"#{channel_id}", subscribed,
            )

    async def list_channels(self) -> list[ChannelDTO]:
        """所有频道(按 id 升序);含未订阅的。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title, username, kind, member_count, created_at, "
                "subscribed, last_synced_at "
                "FROM channels ORDER BY id"
            )
        return [_row_to_channel(r) for r in rows]

    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        """只返 subscribed=TRUE 的频道(按 id 升序);供 MonitorService 喂白名单。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title, username, kind, member_count, created_at, "
                "subscribed, last_synced_at "
                "FROM channels WHERE subscribed = TRUE ORDER BY id"
            )
        return [_row_to_channel(r) for r in rows]

    async def get_channel(self, channel_id: int) -> ChannelDTO | None:
        """单频道;不存在返 None。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, title, username, kind, member_count, created_at, "
                "subscribed, last_synced_at "
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
                "SELECT value FROM meta WHERE key = $1", key,
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
                key, value,
            )

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
                             emoji, download_status, download_error)
                        VALUES
                            ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,
                             $15,$16)
                        """,
                    *_media_to_row(msg_pk, m, idx),
                )
        message.id = msg_pk
        return msg_pk

    async def update_message(self, message: MessageDTO) -> None:
        """代理到 save_message(upsert 语义一致)。"""
        await self.save_message(message)  # upsert 语义一致

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """删单条消息;media 行通过 FK CASCADE 自动删。"""
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
    ) -> list[MessageDTO]:
        """按时间升序 + id 升序(与 Mongo / JSONL 对齐);media 二次查询拼回。

        `channel_ids` 走 ANY($1::bigint[]);`limit` = 最近 N 条(倒序取前 N
        后在内存反转为升序,LIMIT 在 SQL 层)。
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
        # `limit` = 最近 N 条:倒序取前 N 再反转为升序(与 jsonl/mongo 语义一致)。
        order_by = "date DESC, id DESC" if limit is not None else "date ASC, id ASC"
        sql = (
            "SELECT * FROM messages WHERE "
            + " AND ".join(where)
            + f" ORDER BY {order_by}"
        )
        if limit is not None:
            params.append(limit)
            sql += f" LIMIT ${len(params)}"
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

    async def find_media_by_file_id(
        self, telegram_file_id: str
    ) -> MediaDTO | None:
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
            channel_ids, status, media_type, search,
        )
        sort_col = _MEDIA_SORT_COLUMN[sort]
        sql = [
            "SELECT m.*, me.id AS media_id, me.type AS media_type,",
            "       me.mime_type, me.file_name, me.file_size, me.width, me.height,",
            "       me.duration, me.telegram_file_id, me.object_key,",
            "       me.object_backend, me.thumb_key, me.thumb_backend, me.emoji,",
            "       me.download_status AS media_dl_status, me.download_error,",
            "       me.media_idx",
            "FROM messages m JOIN media me ON me.message_id = m.id",
            f"WHERE {where_sql}",
            f"ORDER BY {sort_col} {sort_dir.value.upper()}, m.id DESC, me.media_idx ASC",
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
                "thumb_key": r["thumb_key"],
                "thumb_backend": r["thumb_backend"],
                "emoji": r["emoji"],
                "download_status": r["media_dl_status"],
                "download_error": r["download_error"],
            }
        msg_keys = (
            "id", "channel_id", "telegram_msg_id", "author",
            "date", "text", "views", "forwards", "reply_to_msg_id",
            "edited", "raw",
        )
        return [
            (
                _row_to_message({k: r[k] for k in msg_keys}, [_row_to_media(_row_to_media_dict(r))]),
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
            channel_ids, status, media_type, search,
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
