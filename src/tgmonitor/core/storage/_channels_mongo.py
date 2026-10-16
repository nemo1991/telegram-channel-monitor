"""Mongo 频道子模块 — 2026-10-16 plan-batch8-1.3。

持 11 个频道 + meta method(`upsert_channel` / `upsert_channel_metadata` /
`update_channel_metadata` / `set_channel_subscribed` / `list_channels` /
`list_subscribed_channels` / `get_channel` / `delete_channel` /
`get_max_telegram_msg_id` / `get_meta` / `set_meta`)+ 顶部
`_doc_to_channel` 反序列化 helper。

落地模式:free function + `self` 透传(与 plan-batch8-1.1/1.2
`_lifecycle_mongo` / `_user_meta_mongo` 1:1 对齐)。`MongoRepository`
facade 持 `self._db`,本 module 走 `self.db.X` 反射句柄。

`_doc_to_channel` 是频道反序列化的唯一定义点,被本 module 4 处使用
(list_channels / list_subscribed_channels / get_channel),放本 module
顶部,与频道 method 同生命周期。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from tgmonitor.core.dto import ChannelDTO

if TYPE_CHECKING:
    from tgmonitor.core.storage.mongo_repo import MongoRepository


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


# ---- 11 channel + meta method ----


async def upsert_channel(self: MongoRepository, channel: ChannelDTO) -> None:
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


async def upsert_channel_metadata(self: MongoRepository, channel: ChannelDTO) -> None:
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
    self: MongoRepository,
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


async def set_channel_subscribed(
    self: MongoRepository, channel_id: int, subscribed: bool
) -> None:
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


async def list_channels(self: MongoRepository) -> list[ChannelDTO]:
    """所有频道(按 _id 升序);含未订阅的。"""
    cursor = self.db.channels.find().sort("_id", 1)
    return [_doc_to_channel(d) async for d in cursor]


async def list_subscribed_channels(self: MongoRepository) -> list[ChannelDTO]:
    """只返 subscribed=True 的频道(按 _id 升序);供 MonitorService 喂白名单。"""
    cursor = self.db.channels.find({"subscribed": True}).sort("_id", 1)
    return [_doc_to_channel(d) async for d in cursor]


async def get_channel(self: MongoRepository, channel_id: int) -> ChannelDTO | None:
    """单频道;不存在返 None。"""
    d = await self.db.channels.find_one({"_id": channel_id})
    return _doc_to_channel(d) if d else None


async def delete_channel(self: MongoRepository, channel_id: int) -> None:
    """删频道 + 级联删 messages(media 是 messages 的子文档,无需单独清)。"""
    await self.db.channels.delete_one({"_id": channel_id})
    # 级联删消息(messages.media 子文档内嵌,无需单独 media 集合操作)
    await self.db.messages.delete_many({"channel_id": channel_id})


async def get_max_telegram_msg_id(
    self: MongoRepository, channel_id: int
) -> int | None:
    """续拉历史用 — 该频道已落库的最大 telegram_msg_id;无历史返 None。"""
    d = await self.db.messages.find_one(
        {"channel_id": channel_id},
        sort=[("telegram_msg_id", -1)],
        projection={"telegram_msg_id": 1},
    )
    return int(d["telegram_msg_id"]) if d else None


async def get_meta(self: MongoRepository, key: str) -> str | None:
    """全局单值元数据;不存在返 None。"""
    d = await self.db.meta.find_one({"_id": key})
    return d.get("value") if d else None


async def set_meta(self: MongoRepository, key: str, value: str) -> None:
    """upsert 语义:`update_one` + `upsert=True` 覆盖。"""
    await self.db.meta.update_one(
        {"_id": key},
        {"$set": {"value": value}},
        upsert=True,
    )
