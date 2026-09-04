"""MongoDB 实现 — `motor`(官方 async 驱动)。

- 集合:channels / messages / media / meta
- 唯一索引 `{channel_id, telegram_msg_id}`
- 查询语义与 PostgresRepository 对齐(按 `date ASC, _id ASC` 排序)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from tgmonitor.core.dto import (
    ChannelDTO,
    ChannelStats,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    ReactionDTO,
    SortDir,
    SortKey,
)
from tgmonitor.core.storage.repository import StorageRepository


def _media_status(value: object | None) -> MediaDownloadStatus:
    """反序列化 download_status;非法 / 缺失(旧库)回退 pending。"""
    try:
        return MediaDownloadStatus(str(value))
    except ValueError:
        return MediaDownloadStatus.PENDING


def _media_to_doc(m: MediaDTO) -> dict[str, Any]:
    return {
        "type": m.type.value,
        "mime_type": m.mime_type,
        "file_name": m.file_name,
        "file_size": m.file_size,
        "width": m.width,
        "height": m.height,
        "duration": m.duration,
        "telegram_file_id": m.telegram_file_id,
        "object_key": m.object_key,
        "object_backend": m.object_backend,
        "thumb_key": m.thumb_key,
        "thumb_backend": m.thumb_backend,
        "emoji": m.emoji,
        "download_status": m.download_status.value,
        "download_error": m.download_error,
    }


def _doc_to_media(d: dict[str, Any]) -> MediaDTO:
    return MediaDTO(
        type=MediaType(d["type"]),
        mime_type=d.get("mime_type"),
        file_name=d.get("file_name"),
        file_size=d.get("file_size"),
        width=d.get("width"),
        height=d.get("height"),
        duration=d.get("duration"),
        telegram_file_id=d.get("telegram_file_id"),
        object_key=d.get("object_key"),
        object_backend=d.get("object_backend"),
        thumb_key=d.get("thumb_key"),
        thumb_backend=d.get("thumb_backend"),
        emoji=d.get("emoji"),
        download_status=_media_status(d.get("download_status")),
        download_error=d.get("download_error"),
    )


def _doc_to_channel(d: dict[str, Any]) -> ChannelDTO:
    # 2026-08-31 v1.5.0 PR #A7:channels._id 是 int(沿用 int 主键,与 PG 对齐),
    # 不再 ObjectId 强转。
    return ChannelDTO(
        id=int(d["_id"]),
        title=d["title"],
        username=d.get("username"),
        kind=d.get("kind", "channel"),
        member_count=d.get("member_count"),
        created_at=d.get("created_at"),
        # 旧文档没 subscribed → True(保留"存即订"语义)
        is_subscribed=bool(d.get("subscribed", True)),
        last_synced_at=d.get("last_synced_at"),
        # 2026-09-03 v1.6.0 PR #Q2:Mongo schema-less,旧 doc 无此字段 → None
        photo_local_key=d.get("photo_local_key"),
        # 2026-09-04 v1.6.4:spammer 过滤 UI 用;Mongo schema-less,
        # 旧 doc 无此 4 字段 → 兜底 False,UI 不显示徽标
        is_verified=bool(d.get("is_verified", False)),
        is_scam=bool(d.get("is_scam", False)),
        is_fake=bool(d.get("is_fake", False)),
        has_protected_content=bool(d.get("has_protected_content", False)),
    )


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
        media=[_doc_to_media(m) for m in d.get("media", [])],
        raw=d.get("raw"),
        # 2026-08-27 v1.4.0 PR #9:4 个新字段 — Mongo schema-less,旧 doc
        # 没这些 key,默认 None / False 兜底。
        forward_origin=d.get("forward_origin"),
        via_bot_user_id=d.get("via_bot_user_id"),
        media_album_id=d.get("media_album_id"),
        is_pinned=bool(d.get("is_pinned", False)),
        reactions=reactions,
    )


# 2026-08-25 v1.3.0 PR #6:SortKey → Mongo `$sort` 字段映射。
# DATE:顶层 `date`;SIZE / STATUS:在 `media.*` 子文档(已 unwind)。
_MEDIA_SORT_FIELD: dict[SortKey, str] = {
    SortKey.DATE: "date",
    SortKey.SIZE: "media.file_size",
    SortKey.STATUS: "media.download_status",
}


class MongoRepository(StorageRepository):
    """`_id` 用 `ObjectId`;`id` 字段对 messages 是 ObjectId 的字符串形式。"""

    def __init__(self, dsn: str, database: str = "tgmonitor") -> None:
        """`dsn` = motor DSN;`database` = 库名(默认 `tgmonitor`)。"""
        self._dsn = dsn
        self._db_name = database
        self._client: AsyncIOMotorClient | None = None
        self._db: AsyncIOMotorDatabase | None = None

    @classmethod
    def from_client(
        cls,
        client: AsyncIOMotorClient,
        database: str,
    ) -> MongoRepository:
        """2026-08-31 v1.5.0 PR #A7:用预构造 client 建实例 — 集成测试用。

        生产代码仍走 `__init__(dsn, database)`,走 `connect()` 自建 motor client。
        测试(mongomock_motor / 真 testcontainers mongodb)可传已构造好的对象,
        跳过 motor 的 DSN 解析。

        `from_client` 构造的实例:`close()` 不会关 client(测试可能多个 repo
        共享同一 client;client 生命周期由 fixture 管理)。
        """
        instance = cls.__new__(cls)
        instance._dsn = ""  # sentinel:from_client 路径
        instance._db_name = database
        instance._client = client
        instance._db = client[database]
        return instance

    @property
    def db(self) -> AsyncIOMotorDatabase:
        """当前 DB 句柄;调用前必须 connect()。"""
        assert self._db is not None, "call connect() first"
        return self._db

    # ---- 生命周期 ----

    async def connect(self) -> None:
        """建 motor 客户端 + 拿 db 句柄(不立即 ping)。"""
        if self._client is None:
            self._client = AsyncIOMotorClient(self._dsn)
            self._db = self._client[self._db_name]

    async def close(self) -> None:
        """关 motor 客户端;幂等。`from_client` 构造的实例不会关 client。"""
        if self._dsn and self._client is not None:
            self._client.close()
        self._client = None
        self._db = None

    async def init_schema(self) -> None:
        """建索引:(channel_id, telegram_msg_id) 唯一 / (channel_id, date) /
        (date) / media.message_id / media.telegram_file_id / media.file_name
        (case-insensitive collation) — 幂等(create_index 同名 no-op)。

        2026-08-31 v1.5.0 PR #A7:加 `media.telegram_file_id` 子文档索引 —
        `find_media_by_file_id` 修后走 `$unwind + $match` 路径,需此索引
        走 multikey 命中避免全表扫。旧 `db.media.telegram_file_id` 索引
        保留向后兼容(空集合不影响 query planner)。

        2026-09-03 v1.6.0 PR #Q1:加 `media.file_name` case-insensitive
        collation 索引 — list_messages(search=...) 命中 `$or` 走
        `media.file_name regex` 路径,collation 索引让大小写不敏感匹配
        走 IXSCAN 而非全表扫。mongomock_motor collation 部分支持,集成测试
        见 `test_mongo_repo_parity.py`。
        """
        # 唯一索引
        await self.db.messages.create_index(
            [("channel_id", 1), ("telegram_msg_id", 1)], unique=True
        )
        await self.db.messages.create_index([("channel_id", 1), ("date", 1)])
        await self.db.messages.create_index([("date", 1)])
        # 子文档 multikey 索引 — find_media_by_file_id 用(2026-08-31 PR #A7)
        await self.db.messages.create_index([("media.telegram_file_id", 1)])
        # 2026-09-03 v1.6.0 PR #Q1:file_name collation 索引 — strength=2
        # 表示大小写不敏感、重音敏感,覆盖「搜索大小写不敏感」的 list_messages
        # 行为。collation 名 `en_US` 跟随业务默认(locale 影响排序 / 索引,
        # 与既有 `db.messages` 主索引一致)。
        try:
            await self.db.messages.create_index(
                [("media.file_name", 1)],
                name="idx_messages_media_file_name_ci",
                collation={"locale": "en", "strength": 2},
            )
        except Exception:
            # mongomock_motor 部分版本不支持 collation 参数;集成测试 mongomock
            # 路径可能 IndexOptionsConflict。吞掉,功能仍可用(只是扫表)。
            import logging

            logging.getLogger(__name__).warning(
                "create_index(file_name collation) failed, fallback to table scan",
                exc_info=True,
            )
        # 旧 db.media 索引保留(空集合,无害)
        await self.db.media.create_index([("message_id", 1)])
        await self.db.media.create_index([("telegram_file_id", 1)])

    async def ping(self) -> bool:
        """`db.command("ping")` 探活;任何异常返 False。"""
        try:
            await self.db.command("ping")
            return True
        except Exception:
            return False

    # ---- 频道 ----

    async def upsert_channel(self, channel: ChannelDTO) -> None:
        """全字段 upsert(含 subscribed) — 老调用方兼容;**新代码走 upsert_channel_metadata**。

        2026-09-03 v1.6.0 PR #Q2:加 `photo_local_key` 字段。
        2026-09-04 v1.6.4:加 4 个 spammer 过滤字段。
        """
        doc = {
            "_id": channel.id,
            "title": channel.title,
            "username": channel.username,
            "kind": channel.kind,
            "member_count": channel.member_count,
            "created_at": channel.created_at,
            "first_seen_at": datetime.now(UTC),
            "subscribed": channel.is_subscribed,
            "last_synced_at": channel.last_synced_at,
            "photo_local_key": channel.photo_local_key,
            "is_verified": channel.is_verified,
            "is_scam": channel.is_scam,
            "is_fake": channel.is_fake,
            "has_protected_content": channel.has_protected_content,
        }
        await self.db.channels.update_one({"_id": channel.id}, {"$set": doc}, upsert=True)

    async def upsert_channel_metadata(self, channel: ChannelDTO) -> None:
        """只更元数据字段;subscribed 保持旧值。

        2026-09-03 v1.6.0 PR #Q2:加 `photo_local_key`。
        2026-09-04 v1.6.4:加 4 个 spammer 过滤字段。
        """
        await self.db.channels.update_one(
            {"_id": channel.id},
            {
                "$set": {
                    "title": channel.title,
                    "username": channel.username,
                    "kind": channel.kind,
                    "member_count": channel.member_count,
                    "created_at": channel.created_at,
                    "last_synced_at": channel.last_synced_at,
                    "photo_local_key": channel.photo_local_key,
                    "is_verified": channel.is_verified,
                    "is_scam": channel.is_scam,
                    "is_fake": channel.is_fake,
                    "has_protected_content": channel.has_protected_content,
                }
            },
            upsert=True,
        )

    async def update_channel_metadata(
        self,
        channel_id: int,
        *,
        title: str | None = None,
        username: str | None = None,
        member_count: int | None = None,
        photo_local_key: str | None = None,
        is_verified: bool | None = None,  # 2026-09-04 v1.6.4
        is_scam: bool | None = None,
        is_fake: bool | None = None,
        has_protected_content: bool | None = None,
    ) -> None:
        """2026-08-27 v1.4.0 PR #14:Mongo `$set` 子集 — 只在传入字段上 $set,
        缺省 None 字段不进 dict(保留旧值)。0 rows matched 是合法的
        (TDLib 偶发对陈年 channel 推 metadata update)。

        2026-09-03 v1.6.0 PR #Q2:加 `photo_local_key` 字段。
        2026-09-04 v1.6.4:加 4 个 spammer 过滤字段。
        """
        update: dict[str, Any] = {}
        if title is not None:
            update["title"] = title
        if username is not None:
            update["username"] = username
        if member_count is not None:
            update["member_count"] = member_count
        if photo_local_key is not None:
            update["photo_local_key"] = photo_local_key
        if is_verified is not None:
            update["is_verified"] = is_verified
        if is_scam is not None:
            update["is_scam"] = is_scam
        if is_fake is not None:
            update["is_fake"] = is_fake
        if has_protected_content is not None:
            update["has_protected_content"] = has_protected_content
        if not update:
            return
        await self.db.channels.update_one(
            {"_id": channel_id},
            {"$set": update},
        )

    async def set_channel_subscribed(self, channel_id: int, subscribed: bool) -> None:
        """只设订阅标志;频道未建档时 upsert 一条 stub(后续会被 metadata 覆盖)。"""
        await self.db.channels.update_one(
            {"_id": channel_id},
            {
                "$set": {
                    "subscribed": subscribed,
                    # 首次建档时给个 title,后续会被 metadata 覆盖
                    "title": f"#{channel_id}",
                }
            },
            upsert=True,
        )

    async def list_channels(self) -> list[ChannelDTO]:
        """所有频道(按 _id 升序);含未订阅的。"""
        cursor = self.db.channels.find().sort("_id", 1)
        return [_doc_to_channel(d) async for d in cursor]

    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        """只返 subscribed=True 的频道(按 _id 升序);供 MonitorService 喂白名单。"""
        cursor = self.db.channels.find({"subscribed": True}).sort("_id", 1)
        return [_doc_to_channel(d) async for d in cursor]

    async def get_channel(self, channel_id: int) -> ChannelDTO | None:
        """单频道;不存在返 None。"""
        d = await self.db.channels.find_one({"_id": channel_id})
        return _doc_to_channel(d) if d else None

    async def delete_channel(self, channel_id: int) -> None:
        """删频道 + 级联删 messages(media 是 messages 的子文档,无需单独清)。"""
        await self.db.channels.delete_one({"_id": channel_id})
        # 级联删消息(messages.media 子文档内嵌,无需单独 media 集合操作)
        await self.db.messages.delete_many({"channel_id": channel_id})

    async def get_max_telegram_msg_id(self, channel_id: int) -> int | None:
        """续拉历史用 — 该频道已落库的最大 telegram_msg_id;无历史返 None。"""
        d = await self.db.messages.find_one(
            {"channel_id": channel_id},
            sort=[("telegram_msg_id", -1)],
            projection={"telegram_msg_id": 1},
        )
        return int(d["telegram_msg_id"]) if d else None

    async def get_meta(self, key: str) -> str | None:
        """全局单值元数据;不存在返 None。"""
        d = await self.db.meta.find_one({"_id": key})
        return d.get("value") if d else None

    async def set_meta(self, key: str, value: str) -> None:
        """upsert 语义:`update_one` + `upsert=True` 覆盖。"""
        await self.db.meta.update_one(
            {"_id": key},
            {"$set": {"value": value}},
            upsert=True,
        )

    # ---- 消息 ----

    async def _next_message_id(self) -> int:
        """2026-08-31 v1.5.0 PR #A7:原子计数器 — 给新消息分配 int 主键。

        用 `db.counters` 集合 + `findAndModify` `$inc` 原子自增;与 Postgres
        BIGSERIAL / JsonlFileStore in-memory counter 等价语义。

        修前:`_id` 用 Mongo 默认 ObjectId,`int(str(ObjectId))` 永远
        ValueError(实测 mongomock_motor 24-char hex)。这条路径在生产
        永远炸 — 任何 save_message 调用都不能走通。本 PR 改用 int 计数,
        让 Mongo 与 PG / JSONL 主键语义对齐。
        """
        doc = await self.db.counters.find_one_and_update(
            {"_id": "message_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=True,  # ReturnDocument.AFTER
        )
        return int(doc["seq"])

    async def save_message(self, message: MessageDTO) -> int:
        """幂等 upsert;**显式 int 主键**(PR #A7 改:`_id` 是 int 计数,不再是
        ObjectId)— 与 Postgres / JsonlFileStore 主键语义对齐。

        `message.id` 若已存在(调用方先填):复用,不再分配新计数器。否则
        调 `_next_message_id()` 拿新值。`upsert=True` 时若 (channel_id,
        telegram_msg_id) 已存在,Mongo 用「update path」不会触发 insert,
        主键是已有那行的 — 不会分配新计数器。
        """
        if not message.id:
            message.id = await self._next_message_id()
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
            "media": [_media_to_doc(m) for m in message.media],
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
        }
        # 2026-08-31 v1.5.0 PR #A7:`_id` 用 `$setOnInsert` 而非 `$set` —
        # update 路径下 `_id` 是 immutable,改 `_id` 直接 WriteError 报错
        # (`mongomock_motor: After applying the update, the (immutable)
        # field '_id' was found to have been altered`)。`$setOnInsert` 只在
        # 真的 insert 时生效,update 时复用旧 _id — 与 PG `ON CONFLICT DO
        # NOTHING` / JSONL `replace` 等价语义。
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

    async def update_message(self, message: MessageDTO) -> None:
        """代理到 save_message(upsert 语义一致)。"""
        await self.save_message(message)

    async def update_message_interactions(
        self,
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

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """删单条消息;media 子文档随父 doc 一同删。"""
        await self.db.messages.delete_one(
            {"channel_id": channel_id, "telegram_msg_id": telegram_msg_id}
        )

    async def get_message(self, channel_id: int, telegram_msg_id: int) -> MessageDTO | None:
        """单条消息(media 子文档自动展开);不存在返 None。"""
        d = await self.db.messages.find_one(
            {"channel_id": channel_id, "telegram_msg_id": telegram_msg_id}
        )
        return _doc_to_message(d) if d else None

    async def list_messages(
        self,
        channel_ids: list[int],
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        search: str = "",
    ) -> list[MessageDTO]:
        """按 (date ASC, _id ASC) 排序 — 与 Postgres / JSONL 对齐;`$in` 走 channel_ids。

        `limit` = 最近 N 条(倒序取前 N 后在内存反转为升序)。
        `offset` (v1.4.0 PR #12):从尾部跳过 offset 条再取 limit — 与
        Postgres / JSONL 一致;大 offset 下 `$skip` 是 O(N),docstring
        写明建议收窄 date 过滤。

        `search` (v1.5.1 PR #B2):`$or` 匹配 text 或 media.file_name 子串,
        走 `$regex` + `$options: "i"` 大小写不敏感,`_escape_regex` 防注入。
        """
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
        if search:
            # 复用 list_media 的 `_escape_regex`(已转义 regex 元字符);
            # `text` 与 `media.file_name` 任一命中即过。
            pattern = _escape_regex(search)
            q["$or"] = [
                {"text": {"$regex": pattern, "$options": "i"}},
                {"media.file_name": {"$regex": pattern, "$options": "i"}},
            ]
        if limit is not None:
            # `limit` = 最近 N 条:倒序取前 N 再反转为升序(与 Postgres / JSONL 对齐)。
            # v1.4.0 PR #12:`offset > 0` 时先 `$skip offset` 再 `$limit limit`。
            cursor = (
                self.db.messages.find(q).sort([("date", -1), ("_id", -1)]).skip(offset).limit(limit)
            )
            docs = [d async for d in cursor]
            docs.reverse()
            return [_doc_to_message(d) for d in docs]
        cursor = self.db.messages.find(q).sort([("date", 1), ("_id", 1)])
        return [_doc_to_message(d) async for d in cursor]

    async def count_messages(self, channel_id: int) -> int:
        """该频道已落库消息数(走 count_documents,不应用 date 过滤)。"""
        return await self.db.messages.count_documents({"channel_id": channel_id})

    async def aggregate_per_channel(self, channel_ids: list[int]) -> dict[int, ChannelStats]:
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

    async def find_media_by_file_id(self, telegram_file_id: str) -> MediaDTO | None:
        """跨频道去重:任一已 DONE 的同 file_id media → 返 DTO。

        命中条件 `object_key 非 None AND download_status == 'done'`。返回最新写入
        的那条(`_id` 倒序 → 物理插入序倒序 = upsert 路径下最新优先)。

        2026-08-31 v1.5.0 PR #A7:修 latent bug — media 自 v1.3.0 PR #3 起是
        `db.messages` 子文档(不再单独写 `db.media`),原实现读 `db.media`
        集合永远是空 → 跨频道去重全部不命中。本 PR 改用
        `db.messages.aggregate([$unwind media, $match, $sort])`,与 PG 后端
        `WHERE telegram_file_id = $1 ORDER BY id DESC LIMIT 1` 语义对齐。

        调用方已有「命中即用,不命中走真下载」容错,所以这个 bug 在用户路径
        上表现是「跨频道下载不命中 → 真下载每次重跑」(实际多花 IO 但功能
        正确);修后跨频道去重生效。
        """
        pipeline: list[dict[str, Any]] = [
            {"$unwind": "$media"},
            {
                "$match": {
                    "media.telegram_file_id": telegram_file_id,
                    "media.object_key": {"$ne": None},
                    "media.download_status": "done",
                }
            },
            {"$sort": {"_id": -1}},
            {"$limit": 1},
        ]
        async for doc in self.db.messages.aggregate(pipeline):
            return _doc_to_media(doc["media"])
        return None

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
        """2026-08-25 PR #3:Mongo 后端 list_media — `$unwind messages.media` + `$match`。

        media 在本实现里是 messages 子文档(2026-08-25 决策),所以走
        aggregate pipeline 把每个 message 拆成 N 个「message × media」组合,
        再用 `$match` 应用 status / type / search 过滤。`search` 走
        `file_name` 简单 substring(mongo 后续可上 text index)。

        排序(2026-08-25 v1.3.0 PR #6 新增):`$sort` 字段由 `sort`/`sort_dir`
        决定;tie-breaker `_id DESC, media_idx ASC` 与 Postgres 对齐,
        同 message 内 media 按数组顺序(`idx` 由 `$unwind` 的 includeArrayIndex
        提供)。

        分页:`$skip offset` + `$limit limit`。MVP 不上 cursor-based,offset 在
        Mongo 上对小数据集足够,UI 也没用上 deep paging。
        """
        pipeline = self._build_media_pipeline(
            channel_ids=channel_ids,
            status=status,
            media_type=media_type,
            search=search,
            sort=sort,
            sort_dir=sort_dir,
            offset=offset,
            limit=limit,
        )
        cursor = self.db.messages.aggregate(pipeline)
        rows: list[tuple[MessageDTO, int, MediaDTO]] = []
        async for d in cursor:
            med = _doc_to_media(d["media"])
            # 把媒体子文档从 messages.media 数组临时抽掉,避免 MessageDTO 重复
            d_no_media = {k: v for k, v in d.items() if k != "media"}
            msg = _doc_to_message({**d_no_media, "media": [med]})
            rows.append((msg, int(d["media_idx"]), med))
        return rows

    async def count_media(
        self,
        *,
        channel_ids: list[int] | None = None,
        status: MediaDownloadStatus | None = None,
        media_type: MediaType | None = None,
        search: str = "",
    ) -> int:
        """2026-08-25 v1.3.0 PR #6:与 list_media 同 filter,数总数供分页 UI 用。

        走独立 aggregate,同 match / unwind 路径但无 `$sort` / `$skip` /
        `$limit`,末尾 `$count` 取值(Mongo 5+ 原生支持)。
        """
        pipeline = self._build_media_pipeline(
            channel_ids=channel_ids,
            status=status,
            media_type=media_type,
            search=search,
            sort=SortKey.DATE,
            sort_dir=SortDir.DESC,
            offset=0,
            limit=0,
        )
        # 去掉末尾的 $sort / $skip / $limit(已在 limit=0 时不追加),改 `$count`
        pipeline.append({"$count": "total"})
        async for d in self.db.messages.aggregate(pipeline):
            return int(d.get("total", 0))
        return 0

    def _build_media_pipeline(
        self,
        *,
        channel_ids: list[int] | None,
        status: MediaDownloadStatus | None,
        media_type: MediaType | None,
        search: str,
        sort: SortKey,
        sort_dir: SortDir,
        offset: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """2026-08-25 v1.3.0 PR #6:list_media / count_media 共用的 pipeline 拼装。

        返回 `[{$match}, {$unwind}, {$match media}, {$sort?}, {$skip?}, {$limit?}]`
        — count_media 在此基础上再 append `{$count: total}`。
        """
        pipeline: list[dict[str, Any]] = []
        # 1) 先按 channel 过滤 message(减少 unwind 前集大小)
        if channel_ids:
            pipeline.append({"$match": {"channel_id": {"$in": channel_ids}}})
        # 2) unwind media 子文档数组,includeArrayIndex 给 $idx 用于排序/返回
        pipeline.append(
            {
                "$unwind": {
                    "path": "$media",
                    "includeArrayIndex": "media_idx",
                },
            }
        )
        # 3) media 字段过滤(无 media 的不会被 unwind,自然空)
        media_match: dict[str, Any] = {}
        if status is not None:
            media_match["media.download_status"] = status.value
        if media_type is not None:
            media_match["media.type"] = media_type.value
        if search:
            media_match["media.file_name"] = {
                "$regex": _escape_regex(search),
                "$options": "i",
            }
        if media_match:
            pipeline.append({"$match": media_match})
        # 4) 排序(SortKey → $sort 字段映射;direction 1=ASC, -1=DESC)
        direction = 1 if sort_dir == SortDir.ASC else -1
        sort_field = _MEDIA_SORT_FIELD[sort]
        pipeline.append(
            {
                "$sort": {sort_field: direction, "_id": -1, "media_idx": 1},
            }
        )
        # 5) 偏移 + 限制
        if offset:
            pipeline.append({"$skip": offset})
        if limit:
            pipeline.append({"$limit": limit})
        return pipeline

    async def count_media_by_object_key(self, object_key: str) -> int:
        """2026-08-25 PR #3:refcount — `$unwind` + `$match` + `$count`。

        Mongo 5+ 支持直接 `$count`;走 `messages.media` 子文档。O(扫描)
        + 索引 `media.object_key` 可走 IXSCAN(2026-08-25 未建,后续按需加)。
        """
        pipeline: list[Mapping[str, Any]] = [
            {"$unwind": "$media"},
            {"$match": {"media.object_key": object_key}},
            {"$count": "n"},
        ]
        async for d in self.db.messages.aggregate(pipeline):
            return int(d.get("n", 0))
        return 0

    async def count_media_by_channel(self, channel_id: int) -> int:
        """2026-08-25 v1.3.0 PR #8:该频道全部 media 数(含 PENDING/FAILED)。

        `$match` channel + `$project` 用 `$size media` + `$group sum`。
        """
        pipeline: list[Mapping[str, Any]] = [
            {"$match": {"channel_id": channel_id}},
            {"$project": {"n": {"$size": {"$ifNull": ["$media", []]}}}},
            {"$group": {"_id": None, "total": {"$sum": "$n"}}},
        ]
        async for d in self.db.messages.aggregate(pipeline):
            return int(d.get("total", 0))
        return 0


def _escape_regex(s: str) -> str:
    """转义 Mongo `$regex` 注入(2026-08-25 PR #3)。"""
    import re

    return re.escape(s)
