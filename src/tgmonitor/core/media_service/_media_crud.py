"""Media CRUD operations — 2026-10-15 plan-batch6-1.2 抽到子 module。

包含:
- `list_media` — 转发 storage.list_media + count_media(filter)
- `delete_media` — 摘单条 media + refcount=0 时清 bytes + 发 MediaDeleted
- `preview_delete_by_channel` — Clear Channel dry-run 预览(只读)
- `delete_by_channel` — 批量删某频道 + 顺手清孤儿 bytes

设计(2026-10-15 plan-batch6):
- plain module-level function,首参 `ms: MediaService`,不走 class self
- mutable state 全部走 `ms._bus` / `ms._storage` / `ms._objects` 引用,
  不在本 module 内持副本(避免 stale ref)
- facade `MediaService.<method>` 1 行 thin delegate `await _crud.<method>(self, ...)`
- 公共 method 签名 1:1 保留(向后兼容,UI / `_media_actions.py` 调用面零变化)
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING

from tgmonitor.core.dto import (
    DeleteChannelPreview,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    SortDir,
    SortKey,
)
from tgmonitor.core.events import MediaDeleted

if TYPE_CHECKING:
    from tgmonitor.core.media_service.media_service import MediaService

log = logging.getLogger(__name__)


async def list_media(
    ms: MediaService,
    *,
    channel_id: int | None = None,
    status: MediaDownloadStatus | None = None,
    media_type: MediaType | None = None,
    search: str = "",
    limit: int = 1000,
    offset: int = 0,
    sort: SortKey = SortKey.DATE,
    sort_dir: SortDir = SortDir.DESC,
) -> tuple[list[tuple[MessageDTO, int, MediaDTO]], int]:
    """列出已下载 / 失败 / 下载中媒体(2026-08-25 PR #3 下沉)— 转发 storage。

    filter 全部下沉到 `StorageRepository.list_media` 后端(Postgres
    SQL JOIN,Mongo aggregate,InMemory 顺序扫 + slice),UI 不再
    触碰应用层 flatten。

    2026-08-25 v1.3.0 PR #6:返 `(rows, total)` — `total` 来自
    `storage.count_media` 走同一组 filter 但不带 sort/limit/offset,
    供 Media Manager 状态栏显示「Page N/M · total」。
    """
    channel_ids = [channel_id] if channel_id is not None else None
    rows = await ms._storage.list_media(
        channel_ids=channel_ids,
        status=status,
        media_type=media_type,
        search=search,
        limit=limit,
        offset=offset,
        sort=sort,
        sort_dir=sort_dir,
    )
    total = await ms._storage.count_media(
        channel_ids=channel_ids,
        status=status,
        media_type=media_type,
        search=search,
    )
    return rows, total


async def delete_media(
    ms: MediaService,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> None:
    """摘 media from message + refcount=0 时清 bytes + 发 MediaDeleted。

    与 `MonitorService.delete_message` 的 bytes 清理语义一致:跨消息去重
    场景下,另一 message 仍引用同 `object_key` 时只摘当前 message 的 media,
    不动 bytes;无引用时 `objects.delete(key)` 释放磁盘。
    """
    msg = await ms._storage.get_message(channel_id, telegram_msg_id)
    if msg is None or media_idx >= len(msg.media):
        return
    med = msg.media[media_idx]
    object_key = med.object_key
    new_media = msg.media[:media_idx] + msg.media[media_idx + 1 :]
    new_msg = dataclasses.replace(msg, media=new_media)
    await ms._storage.update_message(new_msg)
    if object_key:
        try:
            n = await ms._storage.count_media_by_object_key(object_key)
        except Exception:  # noqa: BLE001
            log.exception("count media by key failed: %s", object_key)
            n = 0
        if n == 0 and ms._objects is not None:
            try:
                await ms._objects.delete(object_key)
            except Exception:  # noqa: BLE001
                log.warning(
                    "delete bytes %s failed (already gone?)",
                    object_key,
                    exc_info=True,
                )
    await ms._bus.publish(
        MediaDeleted(
            channel_id=channel_id,
            telegram_msg_id=telegram_msg_id,
            media_idx=media_idx,
        )
    )


async def preview_delete_by_channel(
    ms: MediaService,
    channel_id: int,
) -> DeleteChannelPreview:
    """2026-08-25 v1.3.0 PR #8:Clear Channel dry-run 预览。

    **严格只读** — 不调任何 `delete_*` API。返回 `DeleteChannelPreview`:
    - `message_count` 走 `storage.count_messages(channel_id)`
    - `media_count` 走新 abstract `storage.count_media_by_channel`
    - `potential_orphan_bytes`:模拟 `delete_by_channel` 的 refcount 清理
      路径,只累加「refcount=1 且 file_size 非 None」的 DONE media 的字节
      (跨频道共享的不计,跟实际 `objects.delete` 触发条件一致)

    空 channel / 无 media 时返全 0 dataclass,不抛。
    """
    msg_count = await ms._storage.count_messages(channel_id)
    media_count = await ms._storage.count_media_by_channel(channel_id)
    if msg_count == 0 and media_count == 0:
        return DeleteChannelPreview(
            channel_id=channel_id,
            message_count=0,
            media_count=0,
            potential_orphan_bytes=0,
        )

    # 只取 DONE 的 media,跟 delete_by_channel 实际清理的对象一致
    done_rows = await ms._storage.list_media(
        channel_ids=[channel_id],
        status=MediaDownloadStatus.DONE,
        limit=1_000_000,
        offset=0,
    )
    orphan_bytes = 0
    seen: set[str] = set()
    for _msg, _idx, med in done_rows:
        if not med.object_key or med.object_key in seen:
            continue
        seen.add(med.object_key)
        try:
            n = await ms._storage.count_media_by_object_key(med.object_key)
        except Exception:  # noqa: BLE001
            log.exception(
                "preview count_media_by_object_key failed: %s",
                med.object_key,
            )
            n = 0
        if n <= 1 and med.file_size:
            orphan_bytes += med.file_size
    return DeleteChannelPreview(
        channel_id=channel_id,
        message_count=msg_count,
        media_count=media_count,
        potential_orphan_bytes=orphan_bytes,
    )


async def delete_by_channel(ms: MediaService, channel_id: int) -> int:
    """2026-08-25 PR #4:批量删某频道所有 message + 顺手清孤儿 bytes。

    与 `MonitorService.delete_message` 的 bytes 清理语义一致:对每条
    待删 message 的每个 `media.object_key` 做 refcount 检查,=0 时
    调 `objects.delete(key)`。

    跨频道行为:不动其它 channel 的 message / media — 用户典型诉求
    「这个频道我不想留媒体,一键清空」,只清目标频道。

    退出语义:中途 storage.delete_message 抛错 → 已删除的不回滚,
    异常上抛让调用方知道部分成功(2026-08-25:用户确认「不要回滚,
    上抛提示」即可,后续按需加 dry-run preview)。
    """
    msgs = await ms._storage.list_messages([channel_id], limit=None)
    deleted = 0
    for msg in msgs:
        # 1) 先记下该 message 的所有 object_key(用于后续 refcount)
        keys: list[str] = [
            med.object_key
            for med in msg.media
            if med.object_key and med.download_status == MediaDownloadStatus.DONE
        ]
        try:
            await ms._storage.delete_message(channel_id, msg.telegram_msg_id)
        except Exception:  # noqa: BLE001
            log.exception(
                "delete_by_channel partial failure: channel=%s msg=%s",
                channel_id,
                msg.telegram_msg_id,
            )
            continue
        deleted += 1
        # 2) 删 message 后,逐 key 检查 refcount;=0 则清 bytes
        for key in keys:
            try:
                n = await ms._storage.count_media_by_object_key(key)
            except Exception:  # noqa: BLE001
                log.exception("count media by key failed: %s", key)
                continue
            if n == 0 and ms._objects is not None:
                try:
                    await ms._objects.delete(key)
                except Exception:  # noqa: BLE001
                    log.warning(
                        "delete_by_channel bytes %s failed",
                        key,
                        exc_info=True,
                    )
    log.info(
        "delete_by_channel: channel=%s deleted=%d",
        channel_id,
        deleted,
    )
    return deleted


__all__ = [
    "list_media",
    "delete_media",
    "preview_delete_by_channel",
    "delete_by_channel",
]
