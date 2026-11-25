"""SQLite 用户元数据子模块 — 2026-11-25 P2-N1 按域拆分。

持 5 个 user_meta method（与 `_user_meta_pg` 1:1 对齐）：收藏 / 标签 / 备注。
落地模式：free function + `self` 透传。`SqliteRepository` facade 持
`self._db` / `self._lock`，本 module 直接透传使用。

`_row_to_message` 定义在 `_sqlite_common.py`。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tgmonitor.core.dto import MessageDTO
from tgmonitor.core.storage._sqlite_common import _row_to_message

if TYPE_CHECKING:
    from tgmonitor.core.storage.sqlite_repo import SqliteRepository


async def set_favorite(
    self: SqliteRepository, channel_id: int, telegram_msg_id: int, value: bool
) -> None:
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            "UPDATE messages SET is_favorite = ? WHERE channel_id = ? AND telegram_msg_id = ?",
            (value, channel_id, telegram_msg_id),
        )


async def set_tags(
    self: SqliteRepository, channel_id: int, telegram_msg_id: int, tags: list[str]
) -> None:
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            "UPDATE messages SET tags = ? WHERE channel_id = ? AND telegram_msg_id = ?",
            (json.dumps(tags), channel_id, telegram_msg_id),
        )


async def set_notes(
    self: SqliteRepository, channel_id: int, telegram_msg_id: int, notes: str
) -> None:
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            "UPDATE messages SET notes = ? WHERE channel_id = ? AND telegram_msg_id = ?",
            (notes, channel_id, telegram_msg_id),
        )


async def list_favorites(self: SqliteRepository) -> list[MessageDTO]:
    """跨频道 is_favorite=1,date DESC;media 不带(轻量路径,与 PG 对齐)。"""
    assert self._db is not None
    async with self._lock:
        rows = await self._db.execute_fetchall(
            "SELECT * FROM messages WHERE is_favorite = 1 ORDER BY date DESC"
        )
    return [_row_to_message(dict(r), []) for r in rows]


async def list_by_tag(self: SqliteRepository, tag: str) -> list[MessageDTO]:
    """tag 精确匹配(JSON1 json_each),date DESC。"""
    assert self._db is not None
    async with self._lock:
        rows = await self._db.execute_fetchall(
            "SELECT * FROM messages "
            "WHERE EXISTS (SELECT 1 FROM json_each(messages.tags) WHERE value = ?) "
            "ORDER BY date DESC",
            (tag,),
        )
    return [_row_to_message(dict(r), []) for r in rows]
