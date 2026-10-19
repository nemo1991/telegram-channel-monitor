"""PG 用户元数据子模块 — 2026-10-16 plan-batch8-1.2。

持 5 个用户元数据 method(`set_favorite` / `set_tags` / `set_notes` /
`list_favorites` / `list_by_tag`),都写 `messages` 表的 4 个
`2026-09-09 v1.7.2` 新列(`is_favorite` / `tags` / `notes`)。

落地模式:free function + `self` 透传(与 plan-batch8-1.1 `_lifecycle_pg`
+ plan-batch6 media_service 1:1 对齐)。`PostgresRepository` facade
持 `self._pool`,本 module 走 `self._pool.acquire() as conn` 拿连接。

`_row_to_message` 复用 `_messages_pg`(2026-10-19 plan-batch8-1.6
从 facade 搬入 `_messages_pg.py`,顶部 import 同步改为
`from . import _messages_pg as _messages`)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tgmonitor.core.dto import MessageDTO

if TYPE_CHECKING:
    from tgmonitor.core.storage.postgres_repo import PostgresRepository

from tgmonitor.core.storage import _messages_pg as _messages

# ---- 5 user_meta method ----


async def set_favorite(
    self: PostgresRepository, channel_id: int, telegram_msg_id: int, value: bool
) -> None:
    """2026-09-09 v1.7.2:单条设 `is_favorite`。不存在消息 idempotent 不抛。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE messages
            SET is_favorite = $3
            WHERE channel_id = $1 AND telegram_msg_id = $2
            """,
            channel_id,
            telegram_msg_id,
            value,
        )


async def set_tags(
    self: PostgresRepository, channel_id: int, telegram_msg_id: int, tags: list[str]
) -> None:
    """2026-09-09 v1.7.2:单条覆盖式设 `tags`(TEXT[])。idempotent 同上。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE messages
            SET tags = $3::text[]
            WHERE channel_id = $1 AND telegram_msg_id = $2
            """,
            channel_id,
            telegram_msg_id,
            tags,
        )


async def set_notes(
    self: PostgresRepository, channel_id: int, telegram_msg_id: int, notes: str
) -> None:
    """2026-09-09 v1.7.2:单条覆盖式设 `notes`(TEXT)。idempotent 同上。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE messages
            SET notes = $3
            WHERE channel_id = $1 AND telegram_msg_id = $2
            """,
            channel_id,
            telegram_msg_id,
            notes,
        )


async def list_favorites(self: PostgresRepository) -> list[MessageDTO]:
    """2026-09-09 v1.7.2:列所有 `is_favorite=TRUE` 消息,按 date DESC。

    跨频道;media 不带(轻量路径)。
    """
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM messages
            WHERE is_favorite = TRUE
            ORDER BY date DESC
            """
        )
    return [_messages._row_to_message(row, []) for row in rows]


async def list_by_tag(self: PostgresRepository, tag: str) -> list[MessageDTO]:
    """2026-09-09 v1.7.2:按 `tag` 精确匹配查询,按 date DESC。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM messages
            WHERE $1 = ANY(tags)
            ORDER BY date DESC
            """,
            tag,
        )
    return [_messages._row_to_message(row, []) for row in rows]
