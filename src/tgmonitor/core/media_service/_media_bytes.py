"""Media bytes 读取 — 2026-10-15 plan-batch6-1.3 抽到子 module。

包含:
- `load_thumbnail_bytes` — UI 渲染缩略图(thumb 表优先,主图 fallback)
- `load_media_bytes` — Lightbox 原图预览(只读主图)
- `_read_objectstore_bytes` — 内部 helper,统一走 `objects.open_read()`,
  任何异常返 None(thumb miss 是合法状态,DEBUG 而非 WARNING)

设计(2026-10-15 plan-batch6):
- plain module-level function,首参 `ms: MediaService`
- 全量读 bytes 不流式 — 缩略图 ≤ 50KB / 原图 ≤ 30MB 都是单次 GET;
  流式 `open_read iterator` 在这里收益小于复杂度
- 私有 helper `_read_objectstore_bytes` 留本 module 内(2 个公共 method 都用),
  facade 不暴露
- 全量 bytes 走 `objects.open_read(key)`(接口统一,Local / Folder / S3
  都用),失败 try/except 兜底
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO

if TYPE_CHECKING:
    from tgmonitor.core.media_service.media_service import MediaService

log = logging.getLogger(__name__)


async def load_thumbnail_bytes(
    ms: MediaService,
    media: MediaDTO,
    *,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> bytes | None:
    """读 media 的缩略图 bytes — UI 渲染缩略图用(2026-08-25 PR #1)。

    2026-09-29:缩略图独立存储 — 从 `thumbnails` 表拿 thumb DTO(关联键
    `channel_id + msg_id + media_idx` 三元组);原图 `object_key` 仅作
    fallback(thumb 表找不到 / FAILED / DNE 时)。

    退化仅在主图已 DONE 时走;PENDING/FAILED 早返 None 让 UI 走 emoji 占位。
    `channel_id` 必须由 caller 提供(MediaDTO 自身不持 channel_id,保持
    跨边界薄)。

    设计取舍:
    - 全量读 bytes 不流式 — 缩略图一般 ≤ 50KB,本地 FS / S3 都是单次
      GET;流式 (open_read iterator) 在这里收益小于代码复杂度。
    - 不调 LRU 缓存(进程内 UI 层做,service 层不持 Qt 状态)。
    - 用 `objects.open_read(key)` 而非 `get(key)` — 接口统一,Local /
      Folder 后端都用 BytesIO;失败时仍走 try/except 兜底。
    """
    if media.download_status != MediaDownloadStatus.DONE:
        return None
    if ms._objects is None:
        return None
    # 2026-09-29:从独立 thumbnails 表拿 thumb;找不到 → fallback 到主图。
    thumb = await ms._storage.get_thumbnail(channel_id, telegram_msg_id, media_idx)
    if thumb is not None:
        if thumb.download_status != MediaDownloadStatus.DONE:
            return None
        if not thumb.object_key or not thumb.object_backend:
            return None
        return await _read_objectstore_bytes(ms, thumb.object_backend, thumb.object_key)
    # fallback:没 thumb 行 → 尝试主图(老数据兼容,新代码永远有 thumb 表行)
    if not media.object_backend or not media.object_key:
        return None
    return await _read_objectstore_bytes(ms, media.object_backend, media.object_key)


async def _read_objectstore_bytes(ms: MediaService, backend: str, key: str) -> bytes | None:
    """从 ObjectStore 读全量 bytes;任何异常返 None。日志 DEBUG 而非
    WARNING — thumb 读 miss 是合法状态(thumb FAILED/DNE 时不再 read),
    NOT org-conf(2026-09-29 之前 WARNING 噪音让 Media Manager 每次重画
    都刷日志)。
    """
    objects = ms._objects
    if objects is None:
        return None
    try:
        stream = await objects.open_read(key)
    except Exception:  # noqa: BLE001
        log.debug("load_thumbnail_bytes miss: backend=%s key=%s", backend, key)
        return None
    try:
        data = stream.read()
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass
    return data


async def load_media_bytes(ms: MediaService, media: MediaDTO) -> bytes | None:
    """读 media **原图** bytes — Lightbox 全屏预览用(2026-08-31 v1.5.0 PR #A8)。

    与 `load_thumbnail_bytes` 区别:**不优先 thumb_key**,只读 `object_key`
    原图(thumb 90×90 太小,Lightbox 显示会糊)。仅 DONE + 有 objectstore 时
    才读;任何异常返 None。

    设计取舍:
    - 全量读 bytes(同 thumbnail)— 单图通常 ≤ 30MB,本地 FS / S3 都是
      单次 GET;流式读 QtQPixmap 不支持(只能 in-memory decode)
    - GIF / WebP / JPEG / PNG 都原样返回,Qt QPixmap.loadFromData 自动识别
    """
    if media.download_status != MediaDownloadStatus.DONE:
        return None
    if ms._objects is None:
        return None
    backend = media.object_backend
    if not backend:
        return None
    key = media.object_key
    if not key:
        return None
    try:
        stream = await ms._objects.open_read(key)
        try:
            data = stream.read()
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        return data
    except Exception:  # noqa: BLE001
        log.warning(
            "load_media_bytes failed: backend=%s key=%s",
            backend,
            key,
            exc_info=True,
        )
        return None


__all__ = [
    "load_thumbnail_bytes",
    "load_media_bytes",
    "_read_objectstore_bytes",
]
