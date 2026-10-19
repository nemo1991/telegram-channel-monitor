"""Mongo 消息子模块 — 2026-10-19 plan-batch8-1.6。

持 10 个消息 + 互动 method(`save_message` / `update_message` /
`update_message_interactions` / `update_message_pin` / `delete_message` /
`delete_messages` / `get_message` / `list_messages` / `count_messages` /
`aggregate_per_channel`)+ 1 个 mongo 独有的 `_next_message_id` +
顶部 `_doc_to_message` 反序列化 helper。

落地模式:free function + `self` 透传(与 plan-batch8-1.1/1.2/1.3/1.4/1.5
1:1 对齐)。`MongoRepository` facade 持 `self._db`,本 module 走
`self.db.X` 反射句柄。

`_doc_to_message` 是 messages doc→DTO 反序列化的唯一定义点,被本
module 4 处使用(get_message / list_messages / list_favorites 跨子用 /
list_by_tag 跨子用),放本 module 顶部。

`_next_message_id`(2026-08-31 v1.5.0 PR #A7)用 `db.counters` 集合 +
`findAndModify` `$inc` 原子自增,给新消息分配 int 主键;`save_message`
内部调用。

跨子引用:`_media._media_to_doc` / `_media._doc_to_media`(2026-10-19
plan-batch8-1.5 搬到 `_media_mongo` 顶部)。`save_message` 内 media
子文档构造用 `_media._media_to_doc`;`_doc_to_message` 反序列化 media
子文档用 `_media._doc_to_media`。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping

from tgmonitor.core.dto import (
    ChannelStats,
    MessageDTO,
    ReactionDTO,
)
from tgmonitor.core.storage import _media_mongo as _media

if TYPE_CHECKING:
    from tgmonitor.core.storage.mongo_repo import MongoRepository


def _doc_to_message(d: dict[str, Any]) -> MessageDTO:
    # 2026-08-27 v1.4.0 PR #10:reactions 字段 dict 列表 → ReactionDTO;
    # 缺省 None 表示从未推送。
    reactions_raw = d.get("reactions")
    reactions: list[ReactionDTO] | None = (
        [ReactionDTO.from_dict(r) for r in reactions_raw] if reactions_raw is not None else None
    )
    # 2026-08-31 v1.5.0 PR #A7:`_id` 是 int 计数(`save_message._next_message_id`),
    # 直接 `int()`;旧 PR 强转 `int(str(ObjectId))` 在 mongomock / 真 Mongo 都炸。
    return MessageDTO(
        id=int(d["_id"]),
        channel_id=int(d["channel_id"]),
        telegram_msg_id=int(d["telegram_msg_id"]),
        author=d.get("author"),
        date=d["date"],
        text=d.get("text", ""),
        views=d.get("views"),
        forwards=d.get("forwards"),
        reply_to_msg_id=d.get("reply_to_msg_id"),
        edited=bool(d.get("edited", False)),
        media=[_media._doc_to_media(m) for m in d.get("media", [])],
        raw=d.get("raw"),
        # 2026-08-27 v1.4.0 PR #9:4 个新字段 — Mongo schema-less,旧 doc
        # 没这些 key,默认 None / False 兜底。
        forward_origin=d.get("forward_origin"),
        via_bot_user_id=d.get("via_bot_user_id"),
        media_album_id=d.get("media_album_id"),
        is_pinned=bool(d.get("is_pinned", False)),
        reactions=reactions,
        # 2026-09-09 v1.7.2:用户元数据 — Mongo schema-less,旧 doc `.get` 兜底。
        is_favorite=bool(d.get("is_favorite", False)),
        tags=list(d.get("tags") or []),
        notes=d.get("notes") or "",
    )


async def _next_message_id(self: MongoRepository) -> int:
    """2026-08-31 v1.5.0 PR #A7:原子计数器 — 给新消息分配 int 主键。

    用 `db.counters` 集合 + `findAndModify` `$inc` 原子自增;与 Postgres
    BIGSERIAL / InMemoryRepository in-memory counter 等价语义。

    修前:`_id` 用 Mongo 默认 ObjectId,`int(str(ObjectId))` 永远
    ValueError(实测 mongomock_motor 24-char hex)。这条路径在生产
    永远炸 — 任何 save_message 调用都不能走通。本 PR 改用 int 计数,
    让 Mongo 与 PG / InMemory 主键语义对齐。
    """
    doc = await self.db.counters.find_one_and_update(
        {"_id": "message_id"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,  # ReturnDocument.AFTER
    )
    return int(doc["seq"])


# ---- 10 message method ----


async def save_message(self: MongoRepository, message: MessageDTO) -> int:
    """幂等 upsert;**显式 int 主键**(PR #A7 改:`_id` 是 int 计数,不再是
    ObjectId)— 与 Postgres / InMemoryRepository 主键语义对齐。

    `message.id` 若已存在(调用方先填):复用,不再分配新计数器。否则
    调 `_next_message_id()` 拿新值。`upsert=True` 时若 (channel_id,
    telegram_msg_id) 已存在,Mongo 用「update path」不会触发 insert,
    主键是已有那行的 — 不会分配新计数器。
    """
    if not message.id:
        message.id = await _next_message_id(self)
    doc = {
        "_id": message.id,
        "channel_id": message.channel_id,
        "telegram_msg_id": message.telegram_msg_id,
        "author": message.author,
        "date": message.date,
        "text": message.text,
        "views": message.views,
        "forwards": message.forwards,
        "reply_to_msg_id": message.reply_to_msg_id,
        "edited": message.edited,
        "media": [_media._media_to_doc(m) for m in message.media],
        "raw": message.raw,
        # 2026-08-27 v1.4.0 PR #9:4 个新字段。Mongo schema-less,
        # 不需要 migration;旧 doc 没有这些 key,读时 None 兜底。
        "forward_origin": message.forward_origin,
        "via_bot_user_id": message.via_bot_user_id,
        "media_album_id": message.media_album_id,
        "is_pinned": message.is_pinned,
        # 2026-08-27 v1.4.0 PR #10:reactions dict 列表;None 不写 key,
        # [] 写空 list(语义:已推送过但当前空)。
        "reactions": (
            [r.to_dict() for r in message.reactions] if message.reactions is not None else None
        ),
        # 2026-09-09 v1.7.2:用户元数据 — Mongo schema-less 直接写,
        # 旧 doc 缺这些 key 时 `_doc_to_message` 走 `.get` 兜底。
        "is_favorite": message.is_favorite,
        "tags": message.tags,
        "notes": message.notes,
    }
    # 2026-08-31 v1.5.0 PR #A7:`_id` 用 `$setOnInsert` 而非 `$set` —
    # update 路径下 `_id` 是 immutable,改 `_id` 直接 WriteError 报错
    # (`mongomock_motor: After applying the update, the (immutable)
    # field '_id' was found to have been altered`)。`$setOnInsert` 只在
    # 真的 insert 时生效,update 时复用旧 _id — 与 PG `ON CONFLICT DO
    # NOTHING` / InMemory `dict 覆写` 等价语义。
    result = await self.db.messages.find_one_and_update(
        {"channel_id": message.channel_id, "telegram_msg_id": message.telegram_msg_id},
        {
            "$set": {k: v for k, v in doc.items() if k != "_id"},
            "$setOnInsert": {"_id": message.id},
        },
        upsert=True,
        return_document=True,  # ReturnDocument.AFTER
    )
    if result is None:
        # 极端情况(并发):再读一次
        result = await self.db.messages.find_one(
            {"channel_id": message.channel_id, "telegram_msg_id": message.telegram_msg_id}
        )
    message.id = int(result["_id"])
    return message.id


async def update_message(self: MongoRepository, message: MessageDTO) -> None:
    """代理到 save_message(upsert 语义一致)。"""
    await save_message(self, message)


async def update_message_interactions(
    self: MongoRepository,
    channel_id: int,
    telegram_msg_id: int,
    *,
    views: int | None = None,
    reactions: list[Any] | None = None,
) -> None:
    """2026-08-27 v1.4.0 PR #10:Mongo `$set` 部分更新。

    缺省 None 的字段不进 `$set`(保留旧值);空 list reactions 显式
    set 为 `[]` 表示清空。不存在消息 idempotent 不抛(0 matched 是合法的)。
    """
    update: dict[str, Any] = {}
    if views is not None:
        update["views"] = views
    if reactions is not None:
        update["reactions"] = [
            r.to_dict() if isinstance(r, ReactionDTO) else r for r in reactions
        ]
    if not update:
        return
    await self.db.messages.update_one(
        {"channel_id": channel_id, "telegram_msg_id": telegram_msg_id},
        {"$set": update},
    )


async def update_message_pin(
    self: MongoRepository,
    channel_id: int,
    telegram_msg_id: int,
    is_pinned: bool,
) -> None:
    """2026-09-10 v1.7.3:Mongo `$set` 单字段更新。

    schema-less,`is_pinned` 缺省 False(`.get("is_pinned", False)` 在
    `_doc_to_message`)。0 matched 不抛(idempotent)。
    """
    await self.db.messages.update_one(
        {"channel_id": channel_id, "telegram_msg_id": telegram_msg_id},
        {"$set": {"is_pinned": is_pinned}},
    )


async def delete_message(
    self: MongoRepository, channel_id: int, telegram_msg_id: int
) -> None:
    """删单条消息;media 子文档随父 doc 一同删。

    2026-09-29:同时清掉该消息对应的 thumb 文档(thumbnails collection,
    `delete_many` 按 channel_id+msg_id 过滤 — 一条 message 的 thumb 通常
    1 个,但 list_thumbnails_for_message 设计上可能返回多个,所以走 many)。
    """
    await self.db.messages.delete_one(
        {"channel_id": channel_id, "telegram_msg_id": telegram_msg_id}
    )
    await self.db.thumbnails.delete_many(
        {"channel_id": channel_id, "telegram_msg_id": telegram_msg_id}
    )


async def delete_messages(
    self: MongoRepository, channel_id: int, msg_ids: list[int]
) -> None:
    """2026-09-08 v1.7.0:批量删单频道 N 条消息;media 子文档随父 doc 一同删。

    单次 `delete_many` 走 `telegram_msg_id: {$in: [...]}` 索引扫描,
    比 N 次 delete_one 节省 N-1 个 round-trip。

    2026-09-29:同时清掉这 N 条消息对应的 thumb。
    """
    if not msg_ids:
        return
    await self.db.messages.delete_many(
        {
            "channel_id": channel_id,
            "telegram_msg_id": {"$in": msg_ids},
        }
    )
    await self.db.thumbnails.delete_many(
        {
            "channel_id": channel_id,
            "telegram_msg_id": {"$in": msg_ids},
        }
    )


async def get_message(
    self: MongoRepository, channel_id: int, telegram_msg_id: int
) -> MessageDTO | None:
    """单条消息(media 子文档自动展开);不存在返 None。"""
    d = await self.db.messages.find_one(
        {"channel_id": channel_id, "telegram_msg_id": telegram_msg_id}
    )
    return _doc_to_message(d) if d else None


async def list_messages(
    self: MongoRepository,
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
    """按 (date ASC, _id ASC) 排序 — 与 Postgres 对齐;`$in` 走 channel_ids。

    `limit` = 最近 N 条(倒序取前 N 后在内存反转为升序)。
    `offset` (v1.4.0 PR #12):从尾部跳过 offset 条再取 limit — 与
    Postgres 一致;大 offset 下 `$skip` 是 O(N),docstring
    写明建议收窄 date 过滤。

    `search` (v1.5.1 PR #B2):`$or` 匹配 text 或 media.file_name 子串,
    走 `$regex` + `$options: "i"` 大小写不敏感,`_escape_regex` 防注入。

    2026-09-14 v1.7.5 PR #8:`favorite_only` / `tag_only` / `pinned_only`
    走 Mongo `$eq` / `$exists` 子句;AND 语义任一 True 必须命中。
    """
    from tgmonitor.core.storage.mongo_repo import _escape_regex

    if not channel_ids:
        return []
    q: dict[str, Any] = {"channel_id": {"$in": channel_ids}}
    if date_from is not None or date_to is not None:
        date_q: dict[str, Any] = {}
        if date_from is not None:
            date_q["$gte"] = date_from
        if date_to is not None:
            date_q["$lte"] = date_to
        q["date"] = date_q
    if favorite_only:
        q["is_favorite"] = True
    if tag_only:
        # 2026-09-14 v1.7.5 PR #8:`tags` 数组非空 — `$not: {$size: 0}` 判定。
        q["tags"] = {"$not": {"$size": 0}}
    if pinned_only:
        q["is_pinned"] = True
    if search:
        # 复用 list_media 的 `_escape_regex`(已转义 regex 元字符);
        # `text` 与 `media.file_name` 任一命中即过。
        pattern = _escape_regex(search)
        q["$or"] = [
            {"text": {"$regex": pattern, "$options": "i"}},
            {"media.file_name": {"$regex": pattern, "$options": "i"}},
        ]
    if limit is not None:
        # `limit` = 最近 N 条:倒序取前 N 再反转为升序(与 Postgres 对齐)。
        # v1.4.0 PR #12:`offset > 0` 时先 `$skip offset` 再 `$limit limit`。
        cursor = (
            self.db.messages.find(q).sort([("date", -1), ("_id", -1)]).skip(offset).limit(limit)
        )
        docs = [d async for d in cursor]
        docs.reverse()
        return [_doc_to_message(d) for d in docs]
    cursor = self.db.messages.find(q).sort([("date", 1), ("_id", 1)])
    return [_doc_to_message(d) async for d in cursor]


async def count_messages(self: MongoRepository, channel_id: int) -> int:
    """该频道已落库消息数(走 count_documents,不应用 date 过滤)。"""
    return await self.db.messages.count_documents({"channel_id": channel_id})


async def aggregate_per_channel(
    self: MongoRepository, channel_ids: list[int]
) -> dict[int, ChannelStats]:
    """2026-08-27 v1.4.0 PR #15:Mongo $group 单 pipeline 聚合 4 字段。

    注意:本实现 media 是 messages 子文档(2026-08-25 PR #3 决策),
    不在 db.media 集合。`$unwind` + `$group` + `$cond` 数 done_media。
    """
    if not channel_ids:
        return {}
    pipeline: list[Mapping[str, Any]] = [
        {"$match": {"channel_id": {"$in": channel_ids}}},
        {
            "$unwind": {
                "path": "$media",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$channel_id",
                "messages": {"$addToSet": "$_id"},
                "media": {
                    "$sum": {
                        "$cond": [{"$ifNull": ["$media", False]}, 1, 0],
                    }
                },
                "done_media": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$media.download_status", "done"]},
                            1,
                            0,
                        ],
                    }
                },
                "last_date": {"$max": "$date"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "channel_id": "$_id",
                "messages": {"$size": "$messages"},
                "media": 1,
                "done_media": 1,
                "last_date": 1,
            }
        },
    ]
    out: dict[int, ChannelStats] = {}
    async for doc in self.db.messages.aggregate(pipeline):
        cid = int(doc["channel_id"])
        out[cid] = ChannelStats(
            messages=int(doc.get("messages", 0)),
            media=int(doc.get("media", 0)),
            done_media=int(doc.get("done_media", 0)),
            last_date=doc.get("last_date"),
        )
    return out
