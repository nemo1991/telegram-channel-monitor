"""PG 缩略图子模块 — 2026-10-16 plan-batch8-1.4。

持 5 个缩略图 method(`save_thumbnail` / `get_thumbnail` /
`delete_thumbnail` / `list_thumbnails_for_message` /
`count_thumbnails_by_object_key`)+ 顶部 `_row_to_thumbnail`
反序列化 helper。

落地模式:free function + `self` 透传(与 plan-batch8-1.1/1.2/1.3
1:1 对齐)。`PostgresRepository` facade 持 `self._pool`,本 module
走 `self._pool.acquire() as conn` 拿连接。

`_row_to_thumbnail` 是缩略图反序列化的唯一定义点,被本 module 3 处
使用(get_thumbnail / list_thumbnails_for_message + facade 自身后续
如有别处引用),放本 module 顶部。

`_media_status` 反序列化 download_status 是 PG / Mongo 共有 helper,
签名一致但各自定义(plan-batch8 设计决策:不抽公共),本 module 通过
`from tgmonitor.core.storage.postgres_repo import _media_status`
复用 facade 顶部 helper(2026-10-16 plan-batch8-1.5 将搬入
`_media_pg.py`,届时本 module 顶部 import 一并改)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

from tgmonitor.core.dto import ThumbnailDTO

if TYPE_CHECKING:
    from tgmonitor.core.storage.postgres_repo import PostgresRepository

from tgmonitor.core.storage import _media_pg as _media


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
        download_status=_media._media_status(row.get("download_status")),
        download_error=row.get("download_error"),
        telegram_thumb_file_id=row.get("telegram_thumb_file_id"),
    )


# ---- 5 thumbnail method ----


async def save_thumbnail(self: PostgresRepository, thumb: ThumbnailDTO) -> None:
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
    self: PostgresRepository,
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
    self: PostgresRepository,
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
    self: PostgresRepository,
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


async def count_thumbnails_by_object_key(
    self: PostgresRepository, object_key: str
) -> int:
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
