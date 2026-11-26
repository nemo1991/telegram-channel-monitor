"""SQLite 媒体子模块 — 2026-11-25 P2-N1 按域拆分。

持 6 个 media method（与 `_media_pg` 1:1 对齐）。落地模式：free function +
`self` 透传。`SqliteRepository` facade 持 `self._db` / `self._lock`，本 module
直接透传使用。

排序列映射 / 状态序列化（`_MEDIA_SORT_COLUMN` / `_status_to_db`）在共享
`_media_codec.py`;where 构造 / row 映射 helper 在 `_sqlite_common.py`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tgmonitor.core.dto import (
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    SortDir,
    SortKey,
)
from tgmonitor.core.storage._media_codec import _MEDIA_SORT_COLUMN
from tgmonitor.core.storage._sqlite_common import (
    _media_where_clause,
    _row_to_media,
    _row_to_message,
)

if TYPE_CHECKING:
    from tgmonitor.core.storage.sqlite_repo import SqliteRepository


async def find_media_by_file_id(self: SqliteRepository, telegram_file_id: str) -> MediaDTO | None:
    assert self._db is not None
    async with self._lock:
        cur = await self._db.execute(
            """
            SELECT * FROM media
            WHERE telegram_file_id = ?
              AND object_key IS NOT NULL
              AND download_status = 'done'
            ORDER BY id DESC
            LIMIT 1
            """,
            (telegram_file_id,),
        )
        row = await cur.fetchone()
    return _row_to_media(dict(row)) if row else None


async def list_media(
    self: SqliteRepository,
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
    """与 PG 对齐:ROW_NUMBER 窗口生成 media_idx;tie-break m.id DESC, idx ASC。"""
    assert self._db is not None
    where_sql, params = _media_where_clause(channel_ids, status, media_type, search, undownloaded)
    sort_col, nulls_last = _MEDIA_SORT_COLUMN[sort]
    # 2026-11-25 P2-44:SIZE 空值统一用 `{col} IS NULL` 前置键排最后
    # (SQLite 0/1 布尔序),替换原先的 NULLS LAST — 与 PG 端逐字一致,
    # 消除两后端 NULL 排序位置分叉。
    nulls_key = f"{sort_col} IS NULL, " if nulls_last else ""
    sql = (
        "SELECT m.*, me.id AS media_id, me.type AS media_type,"
        "       me.mime_type, me.file_name, me.file_size, me.width, me.height,"
        "       me.duration, me.telegram_file_id, me.object_key,"
        "       me.object_backend, me.thumbnail_telegram_file_id, me.emoji,"
        "       me.download_status AS media_dl_status, me.download_error,"
        "       ROW_NUMBER() OVER (PARTITION BY m.id ORDER BY me.id) - 1 AS media_idx "
        "FROM messages m JOIN media me ON me.message_id = m.id "
        f"WHERE {where_sql} "
        f"ORDER BY {nulls_key}{sort_col} {sort_dir.value.upper()}, m.id DESC, media_idx ASC"
    )
    # 2026-11-25 P2-5:limit=0 语义 = 空结果(不再当"无界")— 原truthiness
    # 判断把 0 当 False 跳过 LIMIT,意外返回全表;与 PG 端同步改 `is not None`。
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    async with self._lock:
        rows = await self._db.execute_fetchall(sql, params)

    out: list[tuple[MessageDTO, int, MediaDTO]] = []
    for r in rows:
        row = dict(r)
        media = _row_to_media(
            {
                "type": row["media_type"],
                "mime_type": row["mime_type"],
                "file_name": row["file_name"],
                "file_size": row["file_size"],
                "width": row["width"],
                "height": row["height"],
                "duration": row["duration"],
                "telegram_file_id": row["telegram_file_id"],
                "object_key": row["object_key"],
                "object_backend": row["object_backend"],
                "emoji": row["emoji"],
                "download_status": row["media_dl_status"],
                "download_error": row["download_error"],
                "thumbnail_telegram_file_id": row["thumbnail_telegram_file_id"],
            }
        )
        out.append((_row_to_message(row, [media]), row["media_idx"], media))
    return out


async def count_media(
    self: SqliteRepository,
    *,
    channel_ids: list[int] | None = None,
    status: MediaDownloadStatus | None = None,
    media_type: MediaType | None = None,
    search: str = "",
    undownloaded: bool = False,
) -> int:
    assert self._db is not None
    where_sql, params = _media_where_clause(channel_ids, status, media_type, search, undownloaded)
    async with self._lock:
        cur = await self._db.execute(
            f"SELECT count(*) FROM messages m "
            f"JOIN media me ON me.message_id = m.id WHERE {where_sql}",
            params,
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def count_media_by_object_key(self: SqliteRepository, object_key: str) -> int:
    assert self._db is not None
    async with self._lock:
        cur = await self._db.execute(
            "SELECT count(*) FROM media WHERE object_key = ?", (object_key,)
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def count_media_by_channel(self: SqliteRepository, channel_id: int) -> int:
    assert self._db is not None
    async with self._lock:
        cur = await self._db.execute(
            "SELECT count(*) FROM media me "
            "JOIN messages m ON me.message_id = m.id WHERE m.channel_id = ?",
            (channel_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def reset_stuck_downloads(self: SqliteRepository) -> int:
    """启动自愈:DOWNLOADING 且无 object_key → pending — 返回复位条数。

    2026-11-23 v2.0.9 hotfix:见 `MediaStore.reset_stuck_downloads` docstring。
    物理值写回 `'pending'`(未下载),读回时由 `_media_status` → None。
    """
    assert self._db is not None
    async with self._lock:
        cur = await self._db.execute(
            "UPDATE media SET download_status = 'pending', download_error = NULL "
            "WHERE download_status = 'downloading' "
            "AND (object_key IS NULL OR object_key = '')"
        )
        return cur.rowcount
