"""Orphan reconcile 单元测试(2026-08-24 新增)。

覆盖 AppService.reconcile_orphans 的 Local / Folder / S3 三条路径:
- dry_run:不删 bytes
- prune 真删:orphan key 消失,referenced key 保留
- S3 后端:NotImplementedError → 当作后端不可用,不崩
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO
from tgmonitor.core.objectstore.folder_store import FolderObjectStore
from tgmonitor.core.objectstore.local_store import LocalObjectStore
from tgmonitor.core.objectstore.s3_store import S3ObjectStore

if TYPE_CHECKING:
    from pathlib import Path

    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.objectstore.base import ObjectStore
    from tgmonitor.core.storage.repository import StorageRepository


# ---- helpers ---------------------------------------------------------

def _done(file_id: str, key: str) -> MediaDTO:
    """构造 DONE 状态 + object_key 的 media。"""
    from tgmonitor.core.dto import MediaType

    return MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name="x.jpg",
        file_size=10,
        telegram_file_id=file_id,
        object_key=key,
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )


def _msg(channel_id: int, msg_id: int, media: list[MediaDTO]):
    from tgmonitor.core.dto import MessageDTO
    return MessageDTO(
        id=0, channel_id=channel_id, telegram_msg_id=msg_id,
        text="", media=media,
    )


# ---- Local 后端 ------------------------------------------------------


@pytest.mark.asyncio
async def test_local_reconcile_dry_run_does_not_delete(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
) -> None:
    """dry_run=True → 不删 ObjectStore bytes。"""
    assert isinstance(objectstore, LocalObjectStore)
    await objectstore.put("media/orphan.jpg", b"orphan", None)
    await storage.save_message(_msg(100, 1, media=[]))  # 没引用
    evt = await app.reconcile_orphans(dry_run=True)
    assert evt.orphans >= 1
    assert evt.deleted == 0
    assert evt.dry_run is True
    assert await objectstore.exists("media/orphan.jpg")


@pytest.mark.asyncio
async def test_local_reconcile_prune_deletes_orphan_bytes(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
) -> None:
    """dry_run=False → 真删 orphan key(referenced 不动)。"""
    assert isinstance(objectstore, LocalObjectStore)
    await objectstore.put("media/orphan.jpg", b"orphan", None)
    await objectstore.put("media/keep.jpg", b"keep", None)
    await storage.save_message(_msg(100, 1, [_done("f-keep", "media/keep.jpg")]))
    evt = await app.reconcile_orphans(dry_run=False)
    assert evt.orphans >= 1
    assert evt.deleted >= 1
    # orphan 删了
    assert not await objectstore.exists("media/orphan.jpg")
    # referenced 还在
    assert await objectstore.exists("media/keep.jpg")


@pytest.mark.asyncio
async def test_local_reconcile_keeps_referenced_bytes(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
) -> None:
    """所有 key 都被 storage 引用 → orphans=0,prune 不删。"""
    assert isinstance(objectstore, LocalObjectStore)
    await objectstore.put("media/only.jpg", b"x", None)
    await storage.save_message(_msg(100, 1, [_done("f", "media/only.jpg")]))
    evt = await app.reconcile_orphans(dry_run=False)
    assert evt.orphans == 0
    assert evt.deleted == 0
    assert await objectstore.exists("media/only.jpg")


# ---- Folder 后端 ----------------------------------------------------


@pytest.mark.asyncio
async def test_folder_reconcile_dry_run_does_not_delete(
    app: AppService, storage: StorageRepository, tmp_path: Path,
) -> None:
    """FolderObjectStore 后端 + dry_run → 不删 bytes。

    Folder 用 2-level shard(`media/ab/cd/<name>`),iter_keys 重组后匹配。
    """
    folder = FolderObjectStore(root=tmp_path / "folder_media")
    await folder.connect()
    saved = app.objects
    app.objects = folder  # type: ignore[assignment]
    try:
        await folder.put("media/orphan.jpg", b"x", None)
        await storage.save_message(_msg(100, 1, media=[]))
        evt = await app.reconcile_orphans(dry_run=True)
        assert evt.backend == "folder"
        assert evt.orphans >= 1
        assert evt.deleted == 0
        assert await folder.exists("media/orphan.jpg")
    finally:
        app.objects = saved  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_folder_reconcile_prune_deletes_orphan_bytes(
    app: AppService, storage: StorageRepository, tmp_path: Path,
) -> None:
    """Folder + dry_run=False → 真删 orphan bytes。"""
    folder = FolderObjectStore(root=tmp_path / "folder_media")
    await folder.connect()
    saved = app.objects
    app.objects = folder  # type: ignore[assignment]
    try:
        await folder.put("media/orphan.jpg", b"x", None)
        evt = await app.reconcile_orphans(dry_run=False)
        assert evt.deleted >= 1
        assert not await folder.exists("media/orphan.jpg")
    finally:
        app.objects = saved  # type: ignore[assignment]


# ---- S3 后端 -------------------------------------------------------


@pytest.mark.asyncio
async def test_s3_reconcile_skips_gracefully(
    app: AppService, storage: StorageRepository,
) -> None:
    """S3 后端 iter_keys raise NotImplementedError → reconcile 当作后端不可用,不崩。

    app.reconcile_orphans 应该捕 NotImplementedError 退化为 scanned=0,
    orphans=0,deleted=0。
    """
    s3 = S3ObjectStore(bucket="test-bucket")
    # 不调 connect() — 跳过真实 SDK 调用
    saved = app.objects
    app.objects = s3  # type: ignore[assignment]
    try:
        evt = await app.reconcile_orphans(dry_run=True)
        assert evt.backend == "s3"
        # scanned / orphans = 0(NotImplementedError → 退化为空集)
        assert evt.scanned == 0
        assert evt.orphans == 0
        assert evt.deleted == 0
    finally:
        app.objects = saved  # type: ignore[assignment]