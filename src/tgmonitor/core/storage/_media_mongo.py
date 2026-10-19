"""Mongo 媒体子模块 — 2026-10-19 plan-batch8-1.5。

持 5 个媒体 method(`find_media_by_file_id` / `list_media` /
`count_media` / `count_media_by_object_key` / `count_media_by_channel`)
+ 1 个同步 pipeline 拼装 helper(`_build_media_pipeline`)
+ 顶部 4 个 helper:`_media_status` / `_media_to_doc` /
`_doc_to_media` / `_MEDIA_SORT_FIELD`。

落地模式:free function + `self` 透传(与 plan-batch8-1.1/1.2/1.3/1.4
1:1 对齐)。`MongoRepository` facade 持 `self._db`,本 module 走
`self.db.X` 反射句柄。

`_media_status`(2026-08-25 PR #3):反序列化 `download_status`,旧库 /
非法值回退 PENDING — `_thumbnails_mongo` 也复用本 module 这一个
(2026-10-19 plan-batch8-1.5 同步从 facade 搬过来,`_thumbnails_mongo`
顶部 import 同步改为 `from . import _media_mongo as _media`)。

`_media_to_doc` / `_doc_to_media` 媒体 DTO ↔ doc 唯一定义点;`save_message`
2026-10-19 plan-batch8-1.6 搬到 `_messages_mongo` 后通过
`from . import _media_mongo as _media` 复用,先在本 module 顶部就位。

`_MEDIA_SORT_FIELD`(2026-08-25 v1.3.0 PR #6):SortKey → Mongo `$sort`
字段映射,DATE 走顶层 `date`;SIZE / STATUS 走 `media.*` 子文档
(已 `$unwind`,无需前缀)。

`_build_media_pipeline`(2026-08-25 v1.3.0 PR #6):list_media / count_media
共用 aggregate pipeline 拼装,返回 `list[dict]`;同步 def 无 await
(纯 list 拼装,无 IO)。count_media 自行 append `{$count: total}` 复用。

`_doc_to_message` 仍暂从 facade 导入 — 2026-10-19 plan-batch8-1.6
将搬入 `_messages_mongo`,届时 list_media 改 `from . import
_messages_mongo as _messages` 复用。

[2026-10-19 plan-batch8-1.6 落地] `_doc_to_message` 已搬到
`_messages_mongo`,本 module 顶部 `from . import _messages_mongo as
_messages` 就位,`list_media` 内调 `_messages._doc_to_message(...)`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from tgmonitor.core.dto import (
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    SortDir,
    SortKey,
)

if TYPE_CHECKING:
    from tgmonitor.core.storage.mongo_repo import MongoRepository

from tgmonitor.core.storage import _messages_mongo as _messages


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
        # 2026-09-29:删 thumb_key / thumb_backend(死字段);留 thumbnail_telegram_file_id
        # — MediaDownloader 用它按需下载缩略图。
        "thumbnail_telegram_file_id": m.thumbnail_telegram_file_id,
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
        # 2026-09-29:thumb_key / thumb_backend 已死;老 doc 残留字段 `.get`
        # 默认 None 即可。thumbnail_telegram_file_id 老 doc 无 → 默认 None。
        emoji=d.get("emoji"),
        download_status=_media_status(d.get("download_status")),
        download_error=d.get("download_error"),
        thumbnail_telegram_file_id=d.get("thumbnail_telegram_file_id"),
    )


# 2026-08-25 v1.3.0 PR #6:SortKey → Mongo `$sort` 字段映射。
# DATE:顶层 `date`;SIZE / STATUS:在 `media.*` 子文档(已 unwind)。
_MEDIA_SORT_FIELD: dict[SortKey, str] = {
    SortKey.DATE: "date",
    SortKey.SIZE: "media.file_size",
    SortKey.STATUS: "media.download_status",
}


# ---- 5 media method + 1 pipeline helper ----


async def find_media_by_file_id(
    self: MongoRepository, telegram_file_id: str
) -> MediaDTO | None:
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
    self: MongoRepository,
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
    pipeline = _build_media_pipeline(
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
        msg = _messages._doc_to_message({**d_no_media, "media": [med]})
        rows.append((msg, int(d["media_idx"]), med))
    return rows


async def count_media(
    self: MongoRepository,
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
    pipeline = _build_media_pipeline(
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
    from tgmonitor.core.storage.mongo_repo import _escape_regex

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


async def count_media_by_object_key(
    self: MongoRepository, object_key: str
) -> int:
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


async def count_media_by_channel(
    self: MongoRepository, channel_id: int
) -> int:
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
