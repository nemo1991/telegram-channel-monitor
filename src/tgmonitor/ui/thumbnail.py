"""缩略图 LRU 缓存(2026-08-25 新增)。

2026-10-01 v1.12.x:从 `ui/widgets/thumbnail_cache.py` 移到 `ui/thumbnail.py`
—— 修 VM 跨层泄漏(`monitor_vm.py:581` 之前从 `widgets.thumbnail_cache`
import `render_pixmap`,VM 本属 ui 顶层,不该下沉到 widgets 子包)。

背景:Media Manager 行内显示 photo 缩略图,直接每次去 ObjectStore 读 bytes +
QPixmap.fromData 会卡(尤其大缩略图 + 多媒体同时出现)。进程内 LRU(200 条)
避免重复加载;photo / video 通用,失败回 None 让 UI 保持 emoji 占位。

设计:
- key = `(backend, object_key)` tuple — backend 拼入避免 local 的"media/x.jpg"
  跟 folder 的"media/x.jpg"命中同一 cache slot 而内容不同(实际不会因为 path
  不一样,但留 defensive)。
- 进程内(`OrderedDict`)LRU,容量 200;命中即 `move_to_end`,setitem 满则 pop
  最旧。
- 不写回 ObjectStore / DB(原 bytes 已在 `object_key`,再生即可)。

非目标(后续 PR):
- 不做 cross-process 共享 / 文件系统缓存。
- 不在 list scroll 时做 LRU eviction 策略优化(可视行优先)。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, ThumbnailDTO

# 缓存容量上限;LRU 超过此值弹最旧。200 条 ≈ 8MB 内存(64×64 RGBA),
# 进程单实例 UI,值得。
DEFAULT_CAPACITY: Final = 200


class ThumbnailCache:
    """进程内 QPixmap LRU。线程安全由 Qt 主线程保证 — 不要在 worker 线程用。"""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = capacity
        # OrderedDict 保持插入顺序;get / put 都 move_to_end 让 LRU 在前
        self._cache: OrderedDict[tuple[str, str], QPixmap] = OrderedDict()

    def get(self, backend: str, object_key: str) -> QPixmap | None:
        """命中率 = LRU 命中(命中返回并 move_to_end);miss 返 None。"""
        key = (backend, object_key)
        pix = self._cache.get(key)
        if pix is None:
            return None
        self._cache.move_to_end(key)
        return pix

    def put(self, backend: str, object_key: str, pix: QPixmap) -> None:
        """写入 LRU;满容量时弹出最旧条目。"""
        key = (backend, object_key)
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = pix
        if len(self._cache) > self._capacity:
            self._cache.popitem(last=False)

    def clear(self) -> None:
        """清空(测试 / 切换 settings 时调)。"""
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


def cache_key_for(media: MediaDTO | ThumbnailDTO) -> tuple[str, str] | None:
    """媒体对象 → cache key;不可显示(None / 非 DONE / 无 key)返 None。

    2026-09-29:签名兼容 `MediaDTO | ThumbnailDTO`(双形态):
    - ThumbnailDTO 走独立 `thumb/<sha256>.<ext>` prefix,优先用之
      (TG 端小缩略图,通常 90×90 JPEG,下载快)
    - MediaDTO 无 thumb 时 fallback 到 `object_key`(原图,大但能显示)

    只接受 DONE 状态 — PENDING/FAILED/DOWNLOADING 时 bytes 还没落地,无图可
    显示;widget 走 emoji fallback 更合适,避免误命中脏数据。
    """
    if media.download_status != MediaDownloadStatus.DONE:
        return None
    if isinstance(media, ThumbnailDTO):
        if not media.object_backend or not media.object_key:
            return None
        return (media.object_backend, media.object_key)
    # MediaDTO 路径
    assert isinstance(media, MediaDTO)
    if not media.object_backend:
        return None
    key = media.object_key
    if not key:
        return None
    return (media.object_backend, key)


def render_pixmap(
    data: bytes,
    *,
    max_size: int = 64,
) -> QPixmap | None:
    """bytes → 缩放后的 QPixmap;失败(非图像格式)返 None。

    用 QPixmap.loadFromData 直接解 JPEG/PNG/WebP;不可解码 → None,UI fallback 到
    emoji。KeepAspectRatio + SmoothTransformation 保证 max_size 内清晰。
    """
    if not data:
        return None
    pix = QPixmap()
    if not pix.loadFromData(data):
        return None
    if pix.isNull():
        return None
    if pix.width() <= max_size and pix.height() <= max_size:
        return pix
    return pix.scaled(
        max_size,
        max_size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
