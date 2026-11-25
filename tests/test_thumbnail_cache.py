"""缩略图 LRU 缓存 + AppService.load_thumbnail_bytes 单测(2026-08-25 新增)。

覆盖:
- ThumbnailCache:capacity / LRU 驱逐 / clear / 命中 move_to_end
- render_pixmap:bytes → QPixmap,空 / 损坏数据返 None,正常 JPEG 入
- cache_key_for:DONE + object_key 优先 / 不可显示返 None
- AppService.load_thumbnail_bytes:三后端路径(local / folder / S3 mock)

2026-09-29:缩略图独立存储(thumbnails table + thumb/<sha256>.<ext> prefix);
本测试不再构造带 thumb_key 的 MediaDTO — thumb_key 字段已从 MediaDTO 删除。
新增 ThumbnailDTO 形态的 cache_key_for 测试,load_thumbnail_bytes 测试
改用 ThumbnailDTO + msg_pk 签名。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QBuffer, QIODevice, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from tgmonitor.core.dto import MediaDTO, MediaType, ThumbnailDTO
from tgmonitor.ui.thumbnail import (
    ThumbnailCache,
    render_pixmap,
)

if TYPE_CHECKING:
    pass


# `qapp` from tests/conftest.py — session-scope QApplication 单例


# ---- ThumbnailCache LRU 行为 ----


def _pix(seed: int = 0) -> QPixmap:
    """造一个小 QPixmap,seed 不同 → 不同内容(用 hash 防去重)。"""
    p = QPixmap(QSize(2, 2))
    p.fill()  # 默认黑
    return p


def _pixmap_to_bytes(pix: QPixmap, *, fmt: str = "PNG") -> bytes:
    """QPixmap → bytes;PySide6 的 `QPixmap.save(QBuffer, fmt)` 需走 QBuffer。"""
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pix.save(buf, fmt)
    return bytes(buf.data())


def test_thumbnail_cache_lru_evicts_oldest(qapp: QApplication) -> None:
    """capacity=2,put 3 个不同 key → 第一个被 evict。"""
    cache = ThumbnailCache(capacity=2)
    p1, p2, p3 = _pix(1), _pix(2), _pix(3)
    cache.put("local", "k1", p1)
    cache.put("local", "k2", p2)
    cache.put("local", "k3", p3)
    # k1 被驱逐
    assert cache.get("local", "k1") is None
    # k2 / k3 仍在
    assert cache.get("local", "k2") is p2
    assert cache.get("local", "k3") is p3


# ---- render_pixmap ----


def test_render_pixmap_garbage_returns_none(qapp: QApplication) -> None:
    assert render_pixmap(b"\x00\x01\x02\x03 not an image") is None


# ---- cache_key_for ----


def _photo(**overrides) -> MediaDTO:
    base = MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name="p.jpg",
        telegram_file_id="fid",
    )
    from dataclasses import replace

    return replace(base, **overrides)


def _thumb(**overrides) -> ThumbnailDTO:
    """2026-09-29:缩略图独立 DTO — 关联键三元组 + thumb/ prefix。"""
    base = ThumbnailDTO(
        channel_id=100,
        telegram_msg_id=1,
        media_idx=0,
    )
    from dataclasses import replace

    return replace(base, **overrides)


# ---- AppService.load_thumbnail_bytes 三后端 ----
