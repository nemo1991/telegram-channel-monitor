"""MongoDB 实现 — `motor`(官方 async 驱动)。

- 集合:channels / messages / media / meta
- 唯一索引 `{channel_id, telegram_msg_id}`
- 查询语义与 PostgresRepository 对齐(按 `date ASC, _id ASC` 排序)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from tgmonitor.core.dto import ChannelDTO, MediaDTO, MediaType, MessageDTO
from tgmonitor.core.storage.repository import StorageRepository


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
    )


def _doc_to_channel(d: dict[str, Any]) -> ChannelDTO:
    return ChannelDTO(
        id=int(d["_id"]),
        title=d["title"],
        username=d.get("username"),
        kind=d.get("kind", "channel"),
        member_count=d.get("member_count"),
        created_at=d.get("created_at"),
    )


def _doc_to_message(d: dict[str, Any]) -> MessageDTO:
    return MessageDTO(
        id=str(d["_id"]),
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


class MongoRepository(StorageRepository):
    """`_id` 用 `ObjectId`;`id` 字段对 messages 是 ObjectId 的字符串形式。"""

    def __init__(self, dsn: str, database: str = "tgmonitor") -> None:
        self._dsn = dsn
        self._db_name = database
        self._client: AsyncIOMotorClient | None = None
        self._db: AsyncIOMotorDatabase | None = None

    @property
    def db(self) -> AsyncIOMotorDatabase:
        assert self._db is not None, "call connect() first"
        return self._db

    # ---- 生命周期 ----

    async def connect(self) -> None:
        self._client = AsyncIOMotorClient(self._dsn)
        self._db = self._client[self._db_name]

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._db = None

    async def init_schema(self) -> None:
        # 唯一索引
        await self.db.messages.create_index(
            [("channel_id", 1), ("telegram_msg_id", 1)], unique=True
        )
        await self.db.messages.create_index([("channel_id", 1), ("date", 1)])
        await self.db.messages.create_index([("date", 1)])
        await self.db.media.create_index([("message_id", 1)])

    async def ping(self) -> bool:
        try:
            await self.db.command("ping")
            return True
        except Exception:
            return False

    # ---- 频道 ----

    async def upsert_channel(self, channel: ChannelDTO) -> None:
        doc = {
            "_id": channel.id,
            "title": channel.title,
            "username": channel.username,
            "kind": channel.kind,
            "member_count": channel.member_count,
            "created_at": channel.created_at,
            "first_seen_at": datetime.utcnow(),
        }
        await self.db.channels.update_one({"_id": channel.id}, {"$set": doc}, upsert=True)

    async def list_channels(self) -> list[ChannelDTO]:
        cursor = self.db.channels.find().sort("_id", 1)
        return [_doc_to_channel(d) async for d in cursor]

    async def get_channel(self, channel_id: int) -> ChannelDTO | None:
        d = await self.db.channels.find_one({"_id": channel_id})
        return _doc_to_channel(d) if d else None

    async def delete_channel(self, channel_id: int) -> None:
        await self.db.channels.delete_one({"_id": channel_id})
        # 级联删消息(messages.media 子文档内嵌,无需单独 media 集合操作)
        await self.db.messages.delete_many({"channel_id": channel_id})

    # ---- 消息 ----

    async def save_message(self, message: MessageDTO) -> int:
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
        message.id = str(result["_id"])
        return message.id  # type: ignore[return-value]

    async def update_message(self, message: MessageDTO) -> None:
        await self.save_message(message)

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        await self.db.messages.delete_one(
            {"channel_id": channel_id, "telegram_msg_id": telegram_msg_id}
        )

    async def get_message(
        self, channel_id: int, telegram_msg_id: int
    ) -> MessageDTO | None:
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
        cursor = self.db.messages.find(q).sort([("date", 1), ("_id", 1)])
        if limit is not None:
            cursor = cursor.limit(limit)
        return [_doc_to_message(d) async for d in cursor]

    async def count_messages(self, channel_id: int) -> int:
        return await self.db.messages.count_documents({"channel_id": channel_id})
