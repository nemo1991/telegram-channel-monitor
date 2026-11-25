"""SQLite 消息子模块 — 2026-11-25 P2-N1 按域拆分。

持 10 个 message + 互动 method（与 `_messages_pg` 1:1 对齐）。落地模式：
free function + `self` 透传。`SqliteRepository` facade 持 `self._db` /
`self._lock`，本 module 直接透传使用。

序列化 / row 映射 helper（`_dt_to_db` / `_json_to_db` / `_media_to_row` /
`_row_to_media` / `_row_to_message` / `_placeholders`）定义在 `_sqlite_common.py`。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from tgmonitor.core.dto import ChannelStats, MediaDTO, MessageDTO, ReactionDTO
from tgmonitor.core.storage._sqlite_common import (
    _dt_from_db,
    _dt_to_db,
    _json_to_db,
    _media_to_row,
    _placeholders,
    _row_to_media,
    _row_to_message,
)

if TYPE_CHECKING:
    from tgmonitor.core.storage.sqlite_repo import SqliteRepository


async def save_message(self: SqliteRepository, message: MessageDTO) -> int:
    """幂等 upsert;隐式建频道占位(与 PG / InMemory 对齐);media 先清后插。

    全程单事务(BEGIN/COMMIT),异常回滚 — 不留「消息在、media 丢」中间态。
    """
    assert self._db is not None
    async with self._lock:
        await self._db.execute("BEGIN")
        try:
            await self._db.execute(
                """
                INSERT INTO channels (id, title, monitored)
                VALUES (?, ?, 0)
                ON CONFLICT (id) DO NOTHING
                """,
                (message.channel_id, f"#{message.channel_id}"),
            )
            cur = await self._db.execute(
                """
                INSERT INTO messages
                    (channel_id, telegram_msg_id, author, date, text,
                     views, forwards, reply_to_msg_id, edited, raw,
                     forward_origin, via_bot_user_id, media_album_id, is_pinned,
                     reactions, is_favorite, tags, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (channel_id, telegram_msg_id) DO UPDATE SET
                    author = excluded.author,
                    date = excluded.date,
                    text = excluded.text,
                    views = excluded.views,
                    forwards = excluded.forwards,
                    reply_to_msg_id = excluded.reply_to_msg_id,
                    edited = excluded.edited,
                    raw = excluded.raw,
                    forward_origin = excluded.forward_origin,
                    via_bot_user_id = excluded.via_bot_user_id,
                    media_album_id = excluded.media_album_id,
                    is_pinned = excluded.is_pinned,
                    reactions = excluded.reactions,
                    is_favorite = excluded.is_favorite,
                    tags = excluded.tags,
                    notes = excluded.notes
                RETURNING id
                """,
                (
                    message.channel_id,
                    message.telegram_msg_id,
                    message.author,
                    _dt_to_db(message.date),
                    message.text,
                    message.views,
                    message.forwards,
                    message.reply_to_msg_id,
                    message.edited,
                    _json_to_db(message.raw),
                    _json_to_db(message.forward_origin),
                    message.via_bot_user_id,
                    message.media_album_id,
                    message.is_pinned,
                    _json_to_db(
                        [r.to_dict() for r in message.reactions]
                        if message.reactions is not None
                        else None
                    ),
                    message.is_favorite,
                    json.dumps(message.tags),
                    message.notes,
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            msg_pk = row["id"]
            await self._db.execute("DELETE FROM media WHERE message_id = ?", (msg_pk,))
            for m in message.media:
                await self._db.execute(
                    """
                    INSERT INTO media
                        (message_id, type, mime_type, file_name, file_size,
                         width, height, duration, telegram_file_id,
                         thumbnail_telegram_file_id,
                         object_key, object_backend,
                         emoji, download_status, download_error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _media_to_row(msg_pk, m),
                )
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise
    message.id = msg_pk
    return msg_pk


async def update_message(self: SqliteRepository, message: MessageDTO) -> None:
    await save_message(self, message)


async def update_message_interactions(
    self: SqliteRepository,
    channel_id: int,
    telegram_msg_id: int,
    *,
    views: int | None = None,
    reactions: list[Any] | None = None,
) -> None:
    """增量更新 views / reactions;None=不动,[]=清空;不存在不抛。"""
    assert self._db is not None
    set_parts: list[str] = []
    args: list[Any] = []
    if views is not None:
        set_parts.append("views = ?")
        args.append(views)
    if reactions is not None:
        set_parts.append("reactions = ?")
        args.append(
            json.dumps([r.to_dict() if isinstance(r, ReactionDTO) else r for r in reactions])
        )
    if not set_parts:
        return
    args.extend([channel_id, telegram_msg_id])
    async with self._lock:
        await self._db.execute(
            f"UPDATE messages SET {', '.join(set_parts)} "
            "WHERE channel_id = ? AND telegram_msg_id = ?",
            args,
        )


async def update_message_pin(
    self: SqliteRepository,
    channel_id: int,
    telegram_msg_id: int,
    is_pinned: bool,
) -> None:
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            "UPDATE messages SET is_pinned = ? WHERE channel_id = ? AND telegram_msg_id = ?",
            (is_pinned, channel_id, telegram_msg_id),
        )


async def delete_message(self: SqliteRepository, channel_id: int, telegram_msg_id: int) -> None:
    """删单条消息;media / thumbnails 走 FK CASCADE。"""
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            "DELETE FROM messages WHERE channel_id = ? AND telegram_msg_id = ?",
            (channel_id, telegram_msg_id),
        )


async def delete_messages(self: SqliteRepository, channel_id: int, msg_ids: list[int]) -> None:
    if not msg_ids:
        return
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            f"DELETE FROM messages WHERE channel_id = ? "
            f"AND telegram_msg_id IN ({_placeholders(len(msg_ids))})",
            [channel_id, *msg_ids],
        )


async def get_message(
    self: SqliteRepository, channel_id: int, telegram_msg_id: int
) -> MessageDTO | None:
    assert self._db is not None
    async with self._lock:
        cur = await self._db.execute(
            "SELECT * FROM messages WHERE channel_id = ? AND telegram_msg_id = ?",
            (channel_id, telegram_msg_id),
        )
        row = await cur.fetchone()
        if not row:
            return None
        media_rows = await self._db.execute_fetchall(
            "SELECT * FROM media WHERE message_id = ? ORDER BY id", (row["id"],)
        )
    return _row_to_message(dict(row), [_row_to_media(dict(m)) for m in media_rows])


async def list_messages(
    self: SqliteRepository,
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
    """与 PG 逐条对齐:limit=最近 N 条(倒序取再反转);offset 仅在 limit 给定
    时生效(从尾部跳过);search 转义 LIKE 通配符,text OR media.file_name。"""
    assert self._db is not None
    if not channel_ids:
        return []
    where: list[str] = [f"channel_id IN ({_placeholders(len(channel_ids))})"]
    params: list[Any] = list(channel_ids)
    if date_from is not None:
        where.append("date >= ?")
        params.append(_dt_to_db(date_from))
    if date_to is not None:
        where.append("date <= ?")
        params.append(_dt_to_db(date_to))
    if favorite_only:
        where.append("is_favorite = 1")
    if tag_only:
        where.append("json_array_length(tags) > 0")
    if pinned_only:
        where.append("is_pinned = 1")
    if search:
        escaped = search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        where.append(
            "(LOWER(text) LIKE ? ESCAPE '\\' "
            "OR EXISTS (SELECT 1 FROM media WHERE media.message_id = messages.id "
            "AND LOWER(media.file_name) LIKE ? ESCAPE '\\'))"
        )
        params.extend([like, like])
    order_by = "date DESC, id DESC" if limit is not None else "date ASC, id ASC"
    sql = "SELECT * FROM messages WHERE " + " AND ".join(where) + f" ORDER BY {order_by}"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
        if offset > 0:
            sql += " OFFSET ?"
            params.append(offset)
    async with self._lock:
        rows = [dict(r) for r in await self._db.execute_fetchall(sql, params)]
        if limit is not None:
            rows.reverse()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        media_rows = await self._db.execute_fetchall(
            f"SELECT * FROM media WHERE message_id IN ({_placeholders(len(ids))}) ORDER BY id",
            ids,
        )
    by_msg: dict[int, list[MediaDTO]] = {}
    for mr in media_rows:
        by_msg.setdefault(mr["message_id"], []).append(_row_to_media(dict(mr)))
    return [_row_to_message(r, by_msg.get(r["id"], [])) for r in rows]


async def count_messages(self: SqliteRepository, channel_id: int) -> int:
    assert self._db is not None
    async with self._lock:
        cur = await self._db.execute(
            "SELECT count(*) FROM messages WHERE channel_id = ?", (channel_id,)
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def aggregate_per_channel(
    self: SqliteRepository, channel_ids: list[int]
) -> dict[int, ChannelStats]:
    """单 GROUP BY 查询;done_media 用 SUM(CASE)(SQLite 无 FILTER 子句)。"""
    if not channel_ids:
        return {}
    assert self._db is not None
    async with self._lock:
        rows = await self._db.execute_fetchall(
            f"""
            SELECT m.channel_id,
                   count(DISTINCT m.id) AS messages,
                   count(me.id) AS media,
                   sum(CASE WHEN me.download_status = 'done' THEN 1 ELSE 0 END)
                       AS done_media,
                   max(m.date) AS last_date
            FROM messages m
            LEFT JOIN media me ON me.message_id = m.id
            WHERE m.channel_id IN ({_placeholders(len(channel_ids))})
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
            last_date=_dt_from_db(r["last_date"]),
        )
    return out
