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

from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MediaType, ThumbnailDTO
from tgmonitor.core.objectstore.folder_store import FolderObjectStore
from tgmonitor.core.objectstore.local_store import LocalObjectStore
from tgmonitor.core.objectstore.s3_store import S3ObjectStore
from tgmonitor.ui.widgets.thumbnail_cache import (
    ThumbnailCache,
    cache_key_for,
    render_pixmap,
)

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.objectstore.base import ObjectStore
    from tgmonitor.core.storage.repository import StorageRepository


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


def test_thumbnail_cache_hit_returns_same_pixmap(qapp: QApplication) -> None:
    cache = ThumbnailCache(capacity=4)
    pix = _pix(1)
    cache.put("local", "media/a.jpg", pix)
    got = cache.get("local", "media/a.jpg")
    assert got is pix


def test_thumbnail_cache_miss_returns_none(qapp: QApplication) -> None:
    cache = ThumbnailCache()
    assert cache.get("local", "missing") is None


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


def test_thumbnail_cache_get_moves_to_end(qapp: QApplication) -> None:
    """命中即更新 LRU 顺序:访问 k1 后 k1 成最新,再 put k3 → 挤掉 k2。"""
    cache = ThumbnailCache(capacity=2)
    cache.put("local", "k1", _pix(1))
    cache.put("local", "k2", _pix(2))
    # 访问 k1(变成最近)
    cache.get("local", "k1")
    # 再 put k3 → 容量满,k2(最久未访问)被挤掉
    cache.put("local", "k3", _pix(3))
    assert cache.get("local", "k2") is None
    assert cache.get("local", "k1") is not None
    assert cache.get("local", "k3") is not None


def test_thumbnail_cache_clear(qapp: QApplication) -> None:
    cache = ThumbnailCache()
    cache.put("local", "k", _pix(1))
    assert len(cache) == 1
    cache.clear()
    assert len(cache) == 0
    assert cache.get("local", "k") is None


# ---- render_pixmap ----


def test_render_pixmap_empty_bytes_returns_none(qapp: QApplication) -> None:
    assert render_pixmap(b"") is None


def test_render_pixmap_garbage_returns_none(qapp: QApplication) -> None:
    assert render_pixmap(b"\x00\x01\x02\x03 not an image") is None


def test_render_pixmap_valid_jpeg_succeeds(qapp: QApplication) -> None:
    """QPixmap → PNG bytes → render_pixmap → 缩小 ≤ 64 的 QPixmap。"""
    src = QPixmap(QSize(8, 8))
    src.fill()  # 黑
    png_bytes = _pixmap_to_bytes(src)
    out = render_pixmap(png_bytes)
    assert out is not None
    assert not out.isNull()
    assert out.width() <= 64 and out.height() <= 64


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


def test_cache_key_thumb_done() -> None:
    """2026-09-29:ThumbnailDTO 走独立 thumb/ prefix,直接 object_key。"""
    t = _thumb(
        object_key="thumb/abc.jpg",
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )
    ck = cache_key_for(t)
    assert ck == ("local", "thumb/abc.jpg")


def test_cache_key_thumb_returns_none_for_pending() -> None:
    t = _thumb(
        object_key="thumb/x.jpg",
        object_backend="local",
        download_status=MediaDownloadStatus.PENDING,
    )
    assert cache_key_for(t) is None


def test_cache_key_falls_back_to_object_key() -> None:
    med = _photo(
        object_key="media/full.jpg",
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )
    ck = cache_key_for(med)
    assert ck == ("local", "media/full.jpg")


def test_cache_key_returns_none_for_pending() -> None:
    med = _photo(
        object_key="media/x.jpg",
        object_backend="local",
        download_status=MediaDownloadStatus.PENDING,
    )
    assert cache_key_for(med) is None


def test_cache_key_returns_none_when_no_key() -> None:
    med = _photo(download_status=MediaDownloadStatus.DONE)
    assert cache_key_for(med) is None


# ---- AppService.load_thumbnail_bytes 三后端 ----


async def test_load_thumbnail_bytes_returns_none_for_failed(
    app: AppService,
    storage: StorageRepository,
) -> None:
    """FAILED media 直接返 None(不读 ObjectStore)。"""
    from tgmonitor.core.dto import MessageDTO

    media = _photo(download_status=MediaDownloadStatus.FAILED)
    msg = MessageDTO(
        id=0,
        channel_id=100,
        telegram_msg_id=1,
        media=[media],
    )
    await storage.save_message(msg)
    assert (
        await app.load_thumbnail_bytes(
            media, channel_id=100, telegram_msg_id=1, media_idx=0
        )
        is None
    )


async def test_load_thumbnail_bytes_local_backend(
    app: AppService,
    storage: StorageRepository,
    objectstore: ObjectStore,
    qapp: QApplication,
) -> None:
    """Local 后端:写 thumb 入独立 thumbs 表 + thumb/ prefix,AppService 读出来。"""
    assert isinstance(objectstore, LocalObjectStore)
    src = QPixmap(QSize(4, 4))
    src.fill()
    png = _pixmap_to_bytes(src)
    await objectstore.put("thumb/abc.png", png, None)

    med = _photo(
        object_key="media/full.png",
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )
    # 2026-09-29:thumb 表落独立行(DONE + object_key)
    await storage.save_thumbnail(
        ThumbnailDTO(
            channel_id=100,
            telegram_msg_id=1,
            media_idx=0,
            object_key="thumb/abc.png",
            object_backend="local",
            download_status=MediaDownloadStatus.DONE,
        )
    )
    out = await app.load_thumbnail_bytes(
        med, channel_id=100, telegram_msg_id=1, media_idx=0
    )
    assert out == png


async def test_load_thumbnail_bytes_folder_backend(
    app: AppService,
    storage: StorageRepository,
    tmp_path,
    qapp: QApplication,
) -> None:
    """Folder 后端:替换 app.objects 后读 thumbnail bytes(走 thumb 表)。"""
    folder = FolderObjectStore(root=tmp_path / "folder_thumb")
    await folder.connect()
    src = QPixmap(QSize(4, 4))
    src.fill()
    png = _pixmap_to_bytes(src)
    await folder.put("thumb/abc.png", png, None)

    saved = app.objects
    app.objects = folder  # type: ignore[assignment]
    try:
        med = _photo(
            object_key="media/full.png",
            object_backend="folder",
            download_status=MediaDownloadStatus.DONE,
        )
        await storage.save_thumbnail(
            ThumbnailDTO(
                channel_id=100,
                telegram_msg_id=1,
                media_idx=0,
                object_key="thumb/abc.png",
                object_backend="folder",
                download_status=MediaDownloadStatus.DONE,
            )
        )
        out = await app.load_thumbnail_bytes(
            med, channel_id=100, telegram_msg_id=1, media_idx=0
        )
        assert out == png
    finally:
        app.objects = saved  # type: ignore[assignment]


async def test_load_thumbnail_bytes_s3_returns_none_when_not_implemented(
    app: AppService,
    storage: StorageRepository,
) -> None:
    """S3 后端 open_read 抛错 → AppService 兜底返 None。"""
    s3 = S3ObjectStore(bucket="test-bucket")
    saved = app.objects
    app.objects = s3  # type: ignore[assignment]
    try:
        med = _photo(
            object_key="media/full.png",
            object_backend="s3",
            download_status=MediaDownloadStatus.DONE,
        )
        await storage.save_thumbnail(
            ThumbnailDTO(
                channel_id=100,
                telegram_msg_id=1,
                media_idx=0,
                object_key="thumb/abc.png",
                object_backend="s3",
                download_status=MediaDownloadStatus.DONE,
            )
        )
        out = await app.load_thumbnail_bytes(
            med, channel_id=100, telegram_msg_id=1, media_idx=0
        )
        assert out is None
    finally:
        app.objects = saved  # type: ignore[assignment]


async def test_load_thumbnail_bytes_missing_key_returns_none(
    app: AppService,
    storage: StorageRepository,
    objectstore: ObjectStore,
) -> None:
    """DONE 但 ObjectStore 里没有 key → open_read 抛 KeyError → 返 None。"""
    assert isinstance(objectstore, LocalObjectStore)
    med = _photo(
        object_key="media/missing.png",
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )
    # 2026-09-29:thumb 表行存在但 object_key 在 ObjectStore 找不到 → 返 None
    await storage.save_thumbnail(
        ThumbnailDTO(
            channel_id=100,
            telegram_msg_id=1,
            media_idx=0,
            object_key="media/missing_thumb.png",
            object_backend="local",
            download_status=MediaDownloadStatus.DONE,
        )
    )
    out = await app.load_thumbnail_bytes(
        med, channel_id=100, telegram_msg_id=1, media_idx=0
    )
    assert out is None


async def test_load_thumbnail_bytes_falls_back_to_object_key_when_no_thumb_row(
    app: AppService,
    storage: StorageRepository,
    objectstore: ObjectStore,
    qapp: QApplication,
) -> None:
    """2026-09-29:thumb 表无行(老数据)→ fallback 到主图 object_key。"""
    assert isinstance(objectstore, LocalObjectStore)
    src = QPixmap(QSize(4, 4))
    src.fill()
    png = _pixmap_to_bytes(src)
    await objectstore.put("media/full.png", png, None)

    med = _photo(
        object_key="media/full.png",
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )
    # thumb 表故意空 — fallback 路径
    out = await app.load_thumbnail_bytes(
        med, channel_id=100, telegram_msg_id=1, media_idx=0
    )
    assert out == png
