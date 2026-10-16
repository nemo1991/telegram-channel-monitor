"""Mongo 用户元数据子模块 — 2026-10-16 plan-batch8-1.2。

持 5 个用户元数据 method(`set_favorite` / `set_tags` / `set_notes` /
`list_favorites` / `list_by_tag`),对应 PG 同 4 个 `2026-09-09 v1.7.2`
用户交互字段(`is_favorite` / `tags` / `notes`)。

落地模式:free function + `self` 透传(与 plan-batch8-1.1 `_lifecycle_mongo`
1:1 对齐)。`MongoRepository` facade 持 `self._db`,本 module 走
`self.db.messages.X` 反射句柄。

`_doc_to_message` 复用在 facade 顶部模块级函数(2026-10-16 plan-batch8-1.6
将搬入 `_messages_mongo.py`,届时本 module 顶部 import 一并改)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tgmonitor.core.dto import MessageDTO

if TYPE_CHECKING:
    from tgmonitor.core.storage.mongo_repo import MongoRepository


# ---- 5 user_meta method ----


async def set_favorite(
    self: MongoRepository, channel_id: int, telegram_msg_id: int, value: bool
) -> None:
    """2026-09-09 v1.7.2:单条设 `is_favorite`。不存在消息 idempotent 不抛。"""
    await self.db.messages.update_one(
        {"channel_id": channel_id, "telegram_msg_id": telegram_msg_id},
        {"$set": {"is_favorite": value}},
    )


async def set_tags(
    self: MongoRepository, channel_id: int, telegram_msg_id: int, tags: list[str]
) -> None:
    """2026-09-09 v1.7.2:单条覆盖式设 `tags`。idempotent 同上。"""
    await self.db.messages.update_one(
        {"channel_id": channel_id, "telegram_msg_id": telegram_msg_id},
        {"$set": {"tags": tags}},
    )


async def set_notes(
    self: MongoRepository, channel_id: int, telegram_msg_id: int, notes: str
) -> None:
    """2026-09-09 v1.7.2:单条覆盖式设 `notes`。idempotent 同上。"""
    await self.db.messages.update_one(
        {"channel_id": channel_id, "telegram_msg_id": telegram_msg_id},
        {"$set": {"notes": notes}},
    )


async def list_favorites(self: MongoRepository) -> list[MessageDTO]:
    """2026-09-09 v1.7.2:列所有 `is_favorite=True` 消息,按 date DESC。"""
    from tgmonitor.core.storage.mongo_repo import _doc_to_message

    cursor = self.db.messages.find({"is_favorite": True}).sort("date", -1)
    return [_doc_to_message(d) for d in await cursor.to_list(length=None)]


async def list_by_tag(self: MongoRepository, tag: str) -> list[MessageDTO]:
    """2026-09-09 v1.7.2:按 `tag` 精确匹配查询(`$in` 走数组包含),按 date DESC。"""
    from tgmonitor.core.storage.mongo_repo import _doc_to_message

    cursor = self.db.messages.find({"tags": tag}).sort("date", -1)
    return [_doc_to_message(d) for d in await cursor.to_list(length=None)]
