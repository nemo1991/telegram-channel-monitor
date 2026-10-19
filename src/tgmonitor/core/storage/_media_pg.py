"""PG 媒体子模块 — 2026-10-19 plan-batch8-1.5。

持 5 个媒体 method(`find_media_by_file_id` / `list_media` /
`count_media` / `count_media_by_object_key` / `count_media_by_channel`)
+ 1 个同步 WHERE 拼装 helper(`_media_where_clause`)
+ 顶部 4 个 helper:`_MEDIA_SORT_COLUMN` / `_media_status` /
`_media_to_row` / `_row_to_media`。

落地模式:free function + `self` 透传(与 plan-batch8-1.1/1.2/1.3/1.4
1:1 对齐)。`PostgresRepository` facade 持 `self._pool`,本 module 走
`self._pool.acquire() as conn` 拿连接。

`_MEDIA_SORT_COLUMN`(2026-08-25 v1.3.0 PR #6):SortKey → SQL ORDER BY
列名映射,DATE 走 m.date;SIZE 走 me.file_size(NULLS LAST);
STATUS 走 me.download_status。`list_media` 排序时按 dir 拼子句。

`_media_status`(2026-08-25 PR #3):反序列化 `download_status`,旧库 /
非法值回退 PENDING — `_thumbnails_pg` 也复用本 module 这一个
(2026-10-19 plan-batch8-1.5 同步从 facade 搬过来,`_thumbnails_pg`
顶部 import 同步改为 `from . import _media_pg as _media`)。

`_media_to_row` / `_row_to_media` 媒体 DTO ↔ row 唯一定义点;`save_message`
2026-10-19 plan-batch8-1.6 搬到 `_messages_pg` 后通过
`from . import _media_pg as _media` 复用 `_media._media_to_row` /
`_media._row_to_media`,先在本 module 顶部就位。

`_media_where_clause`(2026-08-25 v1.3.0 PR #6):list_media / count_media
共用 WHERE 子句拼装,返回 `(where_sql, params, next_idx)` 三元组,
同步 def 无 await(纯字符串拼装,无 IO)。

`list_media` 内部嵌套 `_row_to_media_dict` 局部函数(行 dict →
media 字段映射),跟随 `list_media` 走不出 module 顶部。

`_row_to_message` 仍暂从 facade 导入 — 1.6 plan-batch8 将搬入
`_messages_pg`,届时 list_media 改 `from . import _messages_pg as
_messages` 复用。

[2026-10-19 plan-batch8-1.6 落地] `_row_to_message` 已搬到
`_messages_pg`,本 module 顶部 `from . import _messages_pg as _messages`
就位,`list_media` 内调 `_messages._row_to_message(...)`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import asyncpg

from tgmonitor.core.dto import (
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    SortDir,
    SortKey,
)

if TYPE_CHECKING:
    from tgmonitor.core.storage.postgres_repo import PostgresRepository

from tgmonitor.core.storage import _messages_pg as _messages

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


def _media_status(value: object | None) -> MediaDownloadStatus:
    """反序列化 download_status;非法 / 缺失(旧库)回退 pending。"""
    try:
        return MediaDownloadStatus(str(value))
    except ValueError:
        return MediaDownloadStatus.PENDING


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


# ---- 5 media method + 1 WHERE helper ----


async def find_media_by_file_id(self: PostgresRepository, telegram_file_id: str) -> MediaDTO | None:
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
    self: PostgresRepository,
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
    where_sql, params, next_idx = _media_where_clause(
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
            _messages._row_to_message(
                {k: r[k] for k in msg_keys}, [_row_to_media(_row_to_media_dict(r))]
            ),
            r["media_idx"],
            _row_to_media(_row_to_media_dict(r)),
        )
        for r in rows
    ]


async def count_media(
    self: PostgresRepository,
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
    where_sql, params, _ = _media_where_clause(
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


async def count_media_by_object_key(self: PostgresRepository, object_key: str) -> int:
    """2026-08-25 PR #3:refcount — `SELECT count(*)` 走索引 O(log N)。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*)::int FROM media WHERE object_key = $1",
            object_key,
        )


async def count_media_by_channel(self: PostgresRepository, channel_id: int) -> int:
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
