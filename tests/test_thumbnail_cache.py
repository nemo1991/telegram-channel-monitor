"""缩略图 LRU 缓存 + AppService.load_thumbnail_bytes 单测(2026-08-25 新增)。

覆盖:
- ThumbnailCache:capacity / LRU 驱逐 / clear / 命中 move_to_end
- render_pixmap:bytes → QPixmap,空 / 损坏数据返 None,正常 JPEG 入
- cache_key_for:DONE + thumb_key 优先 / object_key fallback / 不可显示返 None
- AppService.load_thumbnail_bytes:三后端路径(local / folder / S3 mock)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import QBuffer, QIODevice, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MediaType
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


@pytest.fixture
def qt_app() -> QApplication:
    """QPixmap / QWidget 需要 QApplication 存在(QT_QPA_PLATFORM=offscreen)。"""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


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


def test_thumbnail_cache_hit_returns_same_pixmap(qt_app: QApplication) -> None:
    cache = ThumbnailCache(capacity=4)
    pix = _pix(1)
    cache.put("local", "media/a.jpg", pix)
    got = cache.get("local", "media/a.jpg")
    assert got is pix


def test_thumbnail_cache_miss_returns_none(qt_app: QApplication) -> None:
    cache = ThumbnailCache()
    assert cache.get("local", "missing") is None


def test_thumbnail_cache_lru_evicts_oldest(qt_app: QApplication) -> None:
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


def test_thumbnail_cache_get_moves_to_end(qt_app: QApplication) -> None:
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


def test_thumbnail_cache_clear(qt_app: QApplication) -> None:
    cache = ThumbnailCache()
    cache.put("local", "k", _pix(1))
    assert len(cache) == 1
    cache.clear()
    assert len(cache) == 0
    assert cache.get("local", "k") is None


# ---- render_pixmap ----


def test_render_pixmap_empty_bytes_returns_none(qt_app: QApplication) -> None:
    assert render_pixmap(b"") is None


def test_render_pixmap_garbage_returns_none(qt_app: QApplication) -> None:
    assert render_pixmap(b"\x00\x01\x02\x03 not an image") is None


def test_render_pixmap_valid_jpeg_succeeds(qt_app: QApplication) -> None:
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
        type=MediaType.PHOTO, mime_type="image/jpeg", file_name="p.jpg",
        telegram_file_id="fid",
    )
    from dataclasses import replace
    return replace(base, **overrides)


def test_cache_key_prefers_thumb_key() -> None:
    med = _photo(
        object_key="media/full.jpg", object_backend="local",
        thumb_key="media/thumb.jpg", thumb_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )
    ck = cache_key_for(med)
    assert ck == ("local", "media/thumb.jpg")


def test_cache_key_falls_back_to_object_key() -> None:
    med = _photo(
        object_key="media/full.jpg", object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )
    ck = cache_key_for(med)
    assert ck == ("local", "media/full.jpg")


def test_cache_key_returns_none_for_pending() -> None:
    med = _photo(
        object_key="media/x.jpg", object_backend="local",
        download_status=MediaDownloadStatus.PENDING,
    )
    assert cache_key_for(med) is None


def test_cache_key_returns_none_when_no_key() -> None:
    med = _photo(download_status=MediaDownloadStatus.DONE)
    assert cache_key_for(med) is None


# ---- AppService.load_thumbnail_bytes 三后端 ----


@pytest.mark.asyncio
async def test_load_thumbnail_bytes_returns_none_for_failed(
    app: AppService, storage: StorageRepository,
) -> None:
    """FAILED media 直接返 None(不读 ObjectStore)。"""
    from tgmonitor.core.dto import MessageDTO
    media = _photo(download_status=MediaDownloadStatus.FAILED)
    msg = MessageDTO(
        id=0, channel_id=100, telegram_msg_id=1, media=[media],
    )
    await storage.save_message(msg)
    assert await app.load_thumbnail_bytes(media) is None


@pytest.mark.asyncio
async def test_load_thumbnail_bytes_local_backend(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
    qt_app: QApplication,
) -> None:
    """Local 后端:写入 PNG bytes,AppService 读出来。"""
    assert isinstance(objectstore, LocalObjectStore)
    src = QPixmap(QSize(4, 4))
    src.fill()
    png = _pixmap_to_bytes(src)
    await objectstore.put("media/thumb.png", png, None)

    med = _photo(
        object_key="media/thumb.png", object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )
    out = await app.load_thumbnail_bytes(med)
    assert out == png


@pytest.mark.asyncio
async def test_load_thumbnail_bytes_folder_backend(
    app: AppService, storage: StorageRepository, tmp_path,
    qt_app: QApplication,
) -> None:
    """Folder 后端:替换 app.objects 后读 thumbnail bytes。"""
    folder = FolderObjectStore(root=tmp_path / "folder_thumb")
    await folder.connect()
    src = QPixmap(QSize(4, 4))
    src.fill()
    png = _pixmap_to_bytes(src)
    await folder.put("media/thumb.png", png, None)

    saved = app.objects
    app.objects = folder  # type: ignore[assignment]
    try:
        med = _photo(
            object_key="media/thumb.png", object_backend="folder",
            download_status=MediaDownloadStatus.DONE,
        )
        out = await app.load_thumbnail_bytes(med)
        assert out == png
    finally:
        app.objects = saved  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_load_thumbnail_bytes_s3_returns_none_when_not_implemented(
    app: AppService,
) -> None:
    """S3 后端 open_read 抛错 → AppService 兜底返 None。"""
    s3 = S3ObjectStore(bucket="test-bucket")
    saved = app.objects
    app.objects = s3  # type: ignore[assignment]
    try:
        med = _photo(
            object_key="media/thumb.png", object_backend="s3",
            download_status=MediaDownloadStatus.DONE,
        )
        out = await app.load_thumbnail_bytes(med)
        assert out is None
    finally:
        app.objects = saved  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_load_thumbnail_bytes_missing_key_returns_none(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
) -> None:
    """DONE 但 ObjectStore 里没有 key → open_read 抛 KeyError → 返 None。"""
    assert isinstance(objectstore, LocalObjectStore)
    med = _photo(
        object_key="media/missing.png", object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )
    out = await app.load_thumbnail_bytes(med)
    assert out is None


@pytest.mark.asyncio
async def test_load_thumbnail_bytes_uses_thumb_key_first(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
    qt_app: QApplication,
) -> None:
    """优先 thumb_key;thumb 内容应该 ≠ object 内容(我们故意不同)。"""
    assert isinstance(objectstore, LocalObjectStore)
    src = QPixmap(QSize(4, 4))
    src.fill()
    thumb_bytes = _pixmap_to_bytes(src)
    src.fill(0xFFFF0000)  # 改颜色使 thumb/full bytes 不同
    full_bytes = _pixmap_to_bytes(src)

    await objectstore.put("media/thumb.png", thumb_bytes, None)
    await objectstore.put("media/full.png", full_bytes, None)

    med = _photo(
        object_key="media/full.png", object_backend="local",
        thumb_key="media/thumb.png", thumb_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )
    out = await app.load_thumbnail_bytes(med)
    assert out == thumb_bytes  # thumb 优先
