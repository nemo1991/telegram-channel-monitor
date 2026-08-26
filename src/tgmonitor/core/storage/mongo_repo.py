"""MongoDB 实现 — `motor`(官方 async 驱动)。

- 集合:channels / messages / media / meta
- 唯一索引 `{channel_id, telegram_msg_id}`
- 查询语义与 PostgresRepository 对齐(按 `date ASC, _id ASC` 排序)
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from tgmonitor.core.dto import (
    ChannelDTO,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
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
    )


def _doc_to_message(d: dict[str, Any]) -> MessageDTO:
    return MessageDTO(
        id=int(str(d["_id"])),
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

    @property
    def db(self) -> AsyncIOMotorDatabase:
        """当前 DB 句柄;调用前必须 connect()。"""
        assert self._db is not None, "call connect() first"
        return self._db

    # ---- 生命周期 ----

    async def connect(self) -> None:
        """建 motor 客户端 + 拿 db 句柄(不立即 ping)。"""
        self._client = AsyncIOMotorClient(self._dsn)
        self._db = self._client[self._db_name]

    async def close(self) -> None:
        """关 motor 客户端;幂等。"""
        if self._client is not None:
            self._client.close()
            self._client = None
            self._db = None

    async def init_schema(self) -> None:
        """建索引:(channel_id, telegram_msg_id) 唯一 / (channel_id, date) /
        (date) / media.message_id / media.telegram_file_id — 幂等
        (create_index 同名 no-op)。
        """
        # 唯一索引
        await self.db.messages.create_index(
            [("channel_id", 1), ("telegram_msg_id", 1)], unique=True
        )
        await self.db.messages.create_index([("channel_id", 1), ("date", 1)])
        await self.db.messages.create_index([("date", 1)])
        await self.db.media.create_index([("message_id", 1)])
        # 跨消息媒体去重索引(2026-08-24):find_media_by_file_id 用
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
        """全字段 upsert(含 subscribed) — 老调用方兼容;**新代码走 upsert_channel_metadata**。"""
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
        }
        await self.db.channels.update_one({"_id": channel.id}, {"$set": doc}, upsert=True)

    async def upsert_channel_metadata(self, channel: ChannelDTO) -> None:
        """只更元数据字段;subscribed 保持旧值。"""
        await self.db.channels.update_one(
            {"_id": channel.id},
            {"$set": {
                "title": channel.title,
                "username": channel.username,
                "kind": channel.kind,
                "member_count": channel.member_count,
                "created_at": channel.created_at,
                "last_synced_at": channel.last_synced_at,
            }},
            upsert=True,
        )

    async def set_channel_subscribed(
        self, channel_id: int, subscribed: bool
    ) -> None:
        """只设订阅标志;频道未建档时 upsert 一条 stub(后续会被 metadata 覆盖)。"""
        await self.db.channels.update_one(
            {"_id": channel_id},
            {"$set": {
                "subscribed": subscribed,
                # 首次建档时给个 title,后续会被 metadata 覆盖
                "title": f"#{channel_id}",
            }},
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

    async def save_message(self, message: MessageDTO) -> int:
        """幂等 upsert;返回 ObjectId 字符串形式。media 作为子文档内嵌。"""
        # ObjectId 形式的 _id 仍由 Mongo 生成;此处返回 message.id 字符串
        doc = {
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
        }
        result = await self.db.messages.find_one_and_update(
            {"channel_id": message.channel_id, "telegram_msg_id": message.telegram_msg_id},
            {"$set": doc},
            upsert=True,
            return_document=True,  # ReturnDocument.AFTER
        )
        if result is None:
            # 极端情况(并发):再读一次
            result = await self.db.messages.find_one(
                {"channel_id": message.channel_id, "telegram_msg_id": message.telegram_msg_id}
            )
        message.id = int(str(result["_id"]))
        return message.id

    async def update_message(self, message: MessageDTO) -> None:
        """代理到 save_message(upsert 语义一致)。"""
        await self.save_message(message)

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """删单条消息;media 子文档随父 doc 一同删。"""
        await self.db.messages.delete_one(
            {"channel_id": channel_id, "telegram_msg_id": telegram_msg_id}
        )

    async def get_message(
        self, channel_id: int, telegram_msg_id: int
    ) -> MessageDTO | None:
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
    ) -> list[MessageDTO]:
        """按 (date ASC, _id ASC) 排序 — 与 Postgres / JSONL 对齐;`$in` 走 channel_ids。

        `limit` = 最近 N 条(倒序取前 N 后在内存反转为升序)。
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
        if limit is not None:
            # `limit` = 最近 N 条:倒序取前 N 再反转为升序(与 Postgres / JSONL 对齐)。
            cursor = self.db.messages.find(q).sort([("date", -1), ("_id", -1)]).limit(limit)
            docs = [d async for d in cursor]
            docs.reverse()
            return [_doc_to_message(d) for d in docs]
        cursor = self.db.messages.find(q).sort([("date", 1), ("_id", 1)])
        return [_doc_to_message(d) async for d in cursor]

    async def count_messages(self, channel_id: int) -> int:
        """该频道已落库消息数(走 count_documents,不应用 date 过滤)。"""
        return await self.db.messages.count_documents({"channel_id": channel_id})

    async def find_media_by_file_id(
        self, telegram_file_id: str
    ) -> MediaDTO | None:
        """跨频道去重:任一已 DONE 的同 file_id media → 返 DTO。

        命中条件 `object_key 非 None AND download_status == 'done'`。返回最新写入
        的那条(`_id` 倒序 → 物理插入序倒序 = upsert 路径下最新优先)。

        注意:media 在本实现里是 messages 子文档(2026-08-25),此方法从
        `db.media` 集合查 — 现存 latent bug,不在 PR #3 范围;返回的命中会
        一直是 None,调用方已有「命中即用,不命中走真下载」容错。
        """
        doc = await self.db.media.find_one(
            {
                "telegram_file_id": telegram_file_id,
                "object_key": {"$ne": None},
                "download_status": "done",
            },
            sort=[("_id", -1)],
        )
        if doc is None:
            return None
        return _doc_to_media(doc)

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
            channel_ids=channel_ids, status=status,
            media_type=media_type, search=search,
            sort=sort, sort_dir=sort_dir,
            offset=offset, limit=limit,
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
            channel_ids=channel_ids, status=status,
            media_type=media_type, search=search,
            sort=SortKey.DATE, sort_dir=SortDir.DESC,
            offset=0, limit=0,
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
        pipeline.append({
            "$unwind": {
                "path": "$media",
                "includeArrayIndex": "media_idx",
            },
        })
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
        pipeline.append({
            "$sort": {sort_field: direction, "_id": -1, "media_idx": 1},
        })
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
        pipeline = [
            {"$unwind": "$media"},
            {"$match": {"media.object_key": object_key}},
            {"$count": "n"},
        ]
        async for d in self.db.messages.aggregate(pipeline):
            return int(d.get("n", 0))
        return 0


def _escape_regex(s: str) -> str:
    """转义 Mongo `$regex` 注入(2026-08-25 PR #3)。"""
    import re
    return re.escape(s)
