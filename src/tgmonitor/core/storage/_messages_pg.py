"""PG 消息子模块 — 2026-10-19 plan-batch8-1.6。

持 10 个消息 + 互动 method(`save_message` / `update_message` /
`update_message_interactions` / `update_message_pin` / `delete_message` /
`delete_messages` / `get_message` / `list_messages` / `count_messages` /
`aggregate_per_channel`)+ 顶部 `_row_to_message` 反序列化 helper。

落地模式:free function + `self` 透传(与 plan-batch8-1.1/1.2/1.3/1.4/1.5
1:1 对齐)。`PostgresRepository` facade 持 `self._pool`,本 module 走
`self._pool.acquire() as conn` 拿连接。

`_row_to_message` 是 messages 反序列化的唯一定义点,被本 module
2 处使用(get_message / list_messages),放本 module 顶部。

跨子引用:`_media._media_to_row` / `_media._row_to_media`(2026-10-19
plan-batch8-1.5 搬到 `_media_pg` 顶部)。`save_message` 内 `media`
INSERT 用 `_media._media_to_row`;`get_message` / `list_messages` 用
`_media._row_to_media` 反序列化 media row。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

import asyncpg

from tgmonitor.core.dto import (
    ChannelStats,
    MediaDTO,
    MessageDTO,
    ReactionDTO,
)
from tgmonitor.core.storage import _media_pg as _media

if TYPE_CHECKING:
    from tgmonitor.core.storage.postgres_repo import PostgresRepository


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


# ---- 10 message method ----


async def save_message(self: PostgresRepository, message: MessageDTO) -> int:
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
                *_media._media_to_row(msg_pk, m, idx),
            )
    message.id = msg_pk
    return msg_pk


async def update_message(self: PostgresRepository, message: MessageDTO) -> None:
    """代理到 save_message(upsert 语义一致)。"""
    await save_message(self, message)  # upsert 语义一致


async def update_message_interactions(
    self: PostgresRepository,
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
    self: PostgresRepository,
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


async def delete_message(self: PostgresRepository, channel_id: int, telegram_msg_id: int) -> None:
    """删单条消息;media 行通过 FK CASCADE 自动删。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM messages WHERE channel_id = $1 AND telegram_msg_id = $2",
            channel_id,
            telegram_msg_id,
        )


async def delete_messages(self: PostgresRepository, channel_id: int, msg_ids: list[int]) -> None:
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


async def get_message(
    self: PostgresRepository, channel_id: int, telegram_msg_id: int
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
    return _row_to_message(row, [_media._row_to_media(m) for m in media_rows])


async def list_messages(
    self: PostgresRepository,
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
        by_msg.setdefault(mr["message_id"], []).append(_media._row_to_media(mr))
    return [_row_to_message(r, by_msg.get(r["id"], [])) for r in rows]


async def count_messages(self: PostgresRepository, channel_id: int) -> int:
    """该频道已落库消息数;走 COUNT(*) 聚合,不应用 date 过滤。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*)::int FROM messages WHERE channel_id = $1", channel_id
        )


async def aggregate_per_channel(
    self: PostgresRepository, channel_ids: list[int]
) -> dict[int, ChannelStats]:
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
