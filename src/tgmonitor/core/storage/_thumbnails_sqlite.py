"""SQLite 缩略图子模块 — 2026-11-25 P2-N1 按域拆分。

持 5 个 thumbnail method（与 `_thumbnails_pg` 1:1 对齐）。落地模式：
free function + `self` 透传。`SqliteRepository` facade 持 `self._db` /
`self._lock`，本 module 直接透传使用。

`_status_to_db` 在共享 `_media_codec.py`;`_row_to_thumbnail` 在 `_sqlite_common.py`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tgmonitor.core.dto import ThumbnailDTO
from tgmonitor.core.storage._media_codec import _status_to_db
from tgmonitor.core.storage._sqlite_common import _row_to_thumbnail

if TYPE_CHECKING:
    from tgmonitor.core.storage.sqlite_repo import SqliteRepository


async def save_thumbnail(self: SqliteRepository, thumb: ThumbnailDTO) -> None:
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            """
            INSERT INTO thumbnails
                (channel_id, telegram_msg_id, media_idx,
                 object_key, object_backend,
                 mime_type, file_size, width, height, sha256,
                 download_status, download_error, telegram_thumb_file_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (channel_id, telegram_msg_id, media_idx) DO UPDATE SET
                object_key = excluded.object_key,
                object_backend = excluded.object_backend,
                mime_type = excluded.mime_type,
                file_size = excluded.file_size,
                width = excluded.width,
                height = excluded.height,
                sha256 = excluded.sha256,
                download_status = excluded.download_status,
                download_error = excluded.download_error,
                telegram_thumb_file_id = excluded.telegram_thumb_file_id
            """,
            (
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
                _status_to_db(thumb.download_status),
                thumb.download_error,
                thumb.telegram_thumb_file_id,
            ),
        )


async def get_thumbnail(
    self: SqliteRepository,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> ThumbnailDTO | None:
    assert self._db is not None
    async with self._lock:
        cur = await self._db.execute(
            "SELECT * FROM thumbnails "
            "WHERE channel_id = ? AND telegram_msg_id = ? AND media_idx = ?",
            (channel_id, telegram_msg_id, media_idx),
        )
        row = await cur.fetchone()
    return _row_to_thumbnail(dict(row)) if row else None


async def delete_thumbnail(
    self: SqliteRepository,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> None:
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            "DELETE FROM thumbnails WHERE channel_id = ? AND telegram_msg_id = ? AND media_idx = ?",
            (channel_id, telegram_msg_id, media_idx),
        )


async def list_thumbnails_for_message(
    self: SqliteRepository,
    channel_id: int,
    telegram_msg_id: int,
) -> list[ThumbnailDTO]:
    assert self._db is not None
    async with self._lock:
        rows = await self._db.execute_fetchall(
            "SELECT * FROM thumbnails "
            "WHERE channel_id = ? AND telegram_msg_id = ? ORDER BY media_idx",
            (channel_id, telegram_msg_id),
        )
    return [_row_to_thumbnail(dict(r)) for r in rows]


async def count_thumbnails_by_object_key(self: SqliteRepository, object_key: str) -> int:
    assert self._db is not None
    async with self._lock:
        cur = await self._db.execute(
            "SELECT count(*) FROM thumbnails WHERE object_key = ?", (object_key,)
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0]
