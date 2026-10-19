"""Mongo 缩略图子模块 — 2026-10-16 plan-batch8-1.4。

持 5 个缩略图 method(`save_thumbnail` / `get_thumbnail` /
`delete_thumbnail` / `list_thumbnails_for_message` /
`count_thumbnails_by_object_key`)+ 顶部 `_thumb_to_doc` / `_doc_to_thumb`
序列化 + 反序列化 helper。

落地模式:free function + `self` 透传(与 plan-batch8-1.1/1.2/1.3
1:1 对齐)。`MongoRepository` facade 持 `self._db`,本 module 走
`self.db.thumbnails.X` 反射句柄。

`_thumb_to_doc` / `_doc_to_thumb` 是缩略图 doc↔dto 唯一定义点,被本
module 4 处使用(save_thumbnail 用 2 次 + get_thumbnail +
list_thumbnails_for_message),放本 module 顶部。

`_media_status` 反序列化 download_status 是 PG / Mongo 共有 helper,
签名一致但各自定义(plan-batch8 设计决策:不抽公共),本 module 通过
`from tgmonitor.core.storage.mongo_repo import _media_status`
复用 facade 顶部 helper(2026-10-16 plan-batch8-1.5 将搬入
`_media_mongo.py`,届时本 module 顶部 import 一并改)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tgmonitor.core.dto import ThumbnailDTO

if TYPE_CHECKING:
    from tgmonitor.core.storage.mongo_repo import MongoRepository

from tgmonitor.core.storage import _media_mongo as _media


def _thumb_to_doc(t: ThumbnailDTO) -> dict[str, Any]:
    """2026-09-29:thumbnail doc;`_id` 用 `f"{channel_id}:{msg_id}:{media_idx}"`
    复合 string id — MongoDB `_id` 自带唯一索引,免建额外 unique 索引。
    """
    return {
        "_id": f"{t.channel_id}:{t.telegram_msg_id}:{t.media_idx}",
        "channel_id": t.channel_id,
        "telegram_msg_id": t.telegram_msg_id,
        "media_idx": t.media_idx,
        "object_key": t.object_key,
        "object_backend": t.object_backend,
        "file_size": t.file_size,
        "mime_type": t.mime_type,
        "width": t.width,
        "height": t.height,
        "sha256": t.sha256,
        "download_status": t.download_status.value,
        "download_error": t.download_error,
        "telegram_thumb_file_id": t.telegram_thumb_file_id,
    }


def _doc_to_thumb(d: dict[str, Any]) -> ThumbnailDTO:
    """2026-09-29:thumbnail doc → DTO;`_id` 不读(由三 key 重新生成)。"""
    return ThumbnailDTO(
        channel_id=int(d["channel_id"]),
        telegram_msg_id=int(d["telegram_msg_id"]),
        media_idx=int(d["media_idx"]),
        object_key=d.get("object_key"),
        object_backend=d.get("object_backend"),
        file_size=d.get("file_size"),
        mime_type=d.get("mime_type"),
        width=d.get("width"),
        height=d.get("height"),
        sha256=d.get("sha256"),
        download_status=_media._media_status(d.get("download_status")),
        download_error=d.get("download_error"),
        telegram_thumb_file_id=d.get("telegram_thumb_file_id"),
    )


# ---- 5 thumbnail method ----


async def save_thumbnail(self: MongoRepository, thumb: ThumbnailDTO) -> None:
    """upsert thumb(以 _id = `f"{channel_id}:{msg_id}:{media_idx}"` 复合主键)。

    三 key 永远存在(thumb 与父 message 一起产生),主键冲突即可覆盖
    —— `replace_one` upsert 模式。
    """
    await self.db.thumbnails.replace_one(
        {"_id": _thumb_to_doc(thumb)["_id"]},
        _thumb_to_doc(thumb),
        upsert=True,
    )


async def get_thumbnail(
    self: MongoRepository,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> ThumbnailDTO | None:
    """单条 thumb;不存在返 None。"""
    d = await self.db.thumbnails.find_one({"_id": f"{channel_id}:{telegram_msg_id}:{media_idx}"})
    return _doc_to_thumb(d) if d else None


async def delete_thumbnail(
    self: MongoRepository,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> None:
    """不存在不抛(idempotent)。message 删除时由 delete_message 级联调。"""
    await self.db.thumbnails.delete_one({"_id": f"{channel_id}:{telegram_msg_id}:{media_idx}"})


async def list_thumbnails_for_message(
    self: MongoRepository,
    channel_id: int,
    telegram_msg_id: int,
) -> list[ThumbnailDTO]:
    """一条消息的所有 thumb(通常 1 个);按 media_idx ASC。"""
    cursor = self.db.thumbnails.find(
        {"channel_id": channel_id, "telegram_msg_id": telegram_msg_id}
    ).sort("media_idx", 1)
    return [_doc_to_thumb(d) async for d in cursor]


async def count_thumbnails_by_object_key(self: MongoRepository, object_key: str) -> int:
    """orphan reconcile 用:被 storage 引用的 thumb key 计数(>0 = 不该删)。"""
    return await self.db.thumbnails.count_documents({"object_key": object_key})
