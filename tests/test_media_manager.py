"""Media Manager 单元测试(2026-08-24 新增)。

覆盖 AppService 层的 media 管理方法:
- list_media:filter 组合
- delete_media:refcount 路径
- retry_media:reset + force 重下
- open_media:Local / Folder / S3 路径分支
- 事件发布(MediaDeleted / MediaRetried / MediaDownloaded)
"""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest

from tgmonitor.core.dto import (
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
)
from tgmonitor.core.events import (
    MediaDeleted,
    MediaDownloaded,
    MediaReconcileFinished,
    MediaRetried,
)
from tgmonitor.core.objectstore.folder_store import FolderObjectStore

if TYPE_CHECKING:
    from pathlib import Path

    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.events import EventBus
    from tgmonitor.core.objectstore.base import ObjectStore
    from tgmonitor.core.storage.repository import StorageRepository


# ---- helpers ---------------------------------------------------------

def _base_media(file_id: str = "fid-x", **overrides) -> MediaDTO:
    """构造带 telegram_file_id 的 PHOTO media — 测试用基础形态。"""
    base = MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name="photo.jpg",
        file_size=1024,
        telegram_file_id=file_id,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _done_media(file_id: str, key: str) -> MediaDTO:
    """DONE 状态 + object_key 的 media(模拟已下载完成)。"""
    return dataclasses.replace(
        _base_media(file_id),
        object_key=key,
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )


def _failed_media(file_id: str) -> MediaDTO:
    """FAILED 状态(等用户 retry)。"""
    return dataclasses.replace(
        _base_media(file_id),
        download_status=MediaDownloadStatus.FAILED,
        download_error="network timeout",
    )


def _msg(channel_id: int, msg_id: int, media: list[MediaDTO]) -> MessageDTO:
    return MessageDTO(
        id=0,
        channel_id=channel_id,
        telegram_msg_id=msg_id,
        text="hi",
        media=media,
    )


# ---- list_media ------------------------------------------------------


@pytest.mark.asyncio
async def test_list_media_returns_all_with_no_filter(
    app: AppService, storage: StorageRepository,
) -> None:
    """无 filter 时返所有 media(扁平化)。"""
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    await storage.save_message(_msg(100, 2, [_done_media("f2", "media/2.jpg")]))
    rows = await app.list_media()
    assert len(rows) == 2
    keys = {(r[0].channel_id, r[0].telegram_msg_id, r[1]) for r in rows}
    assert keys == {(100, 1, 0), (100, 2, 0)}


@pytest.mark.asyncio
async def test_list_media_filters_by_status_failed(
    app: AppService, storage: StorageRepository,
) -> None:
    """status=FAILED 只返失败的 media。"""
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    await storage.save_message(_msg(100, 2, [_failed_media("f2")]))
    rows = await app.list_media(status=MediaDownloadStatus.FAILED)
    assert len(rows) == 1
    assert rows[0][2].download_status == MediaDownloadStatus.FAILED


@pytest.mark.asyncio
async def test_list_media_filters_by_channel(
    app: AppService, storage: StorageRepository,
) -> None:
    """channel_id 过滤 — 只返指定频道。"""
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    await storage.save_message(_msg(200, 1, [_done_media("f2", "media/2.jpg")]))
    rows = await app.list_media(channel_id=100)
    assert len(rows) == 1
    assert rows[0][0].channel_id == 100


@pytest.mark.asyncio
async def test_list_media_filters_by_type(
    app: AppService, storage: StorageRepository,
) -> None:
    """media_type 过滤 — 只返指定类型。"""
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    video = dataclasses.replace(
        _base_media("f2"), type=MediaType.VIDEO, file_name="vid.mp4",
        mime_type="video/mp4",
    )
    await storage.save_message(_msg(100, 2, [video]))
    rows = await app.list_media(media_type=MediaType.VIDEO)
    assert len(rows) == 1
    assert rows[0][2].type == MediaType.VIDEO


@pytest.mark.asyncio
async def test_list_media_search_by_filename_case_insensitive(
    app: AppService, storage: StorageRepository,
) -> None:
    """filename 搜索大小写不敏感。"""
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    sunset = dataclasses.replace(
        _done_media("f2", "media/2.jpg"), file_name="Sunset.JPG",
    )
    await storage.save_message(_msg(100, 2, [sunset]))
    await storage.save_message(_msg(100, 3, [_base_media("f3")]))
    rows = await app.list_media(search="sunset")
    assert len(rows) == 1
    assert rows[0][2].file_name == "Sunset.JPG"


# ---- delete_media ----------------------------------------------------


@pytest.mark.asyncio
async def test_delete_media_removes_from_message_keeps_bytes_when_referenced(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
) -> None:
    """同 key 两条 message 都引用 — 删一条 → media 摘掉,bytes 保留。"""
    await objectstore.put("media/shared.jpg", b"data", None)
    shared = _done_media("f-shared", "media/shared.jpg")
    await storage.save_message(_msg(100, 1, [shared]))
    await storage.save_message(_msg(100, 2, [shared]))  # 同 media 对象引用
    await app.delete_media(100, 1, 0)

    # message 1 已删 media;message 2 仍引用
    m1 = await storage.get_message(100, 1)
    m2 = await storage.get_message(100, 2)
    assert m1 is not None and len(m1.media) == 0
    assert m2 is not None and len(m2.media) == 1
    # bytes 保留(被 message 2 引用)
    assert await objectstore.exists("media/shared.jpg")


@pytest.mark.asyncio
async def test_delete_media_removes_bytes_when_no_other_reference(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
) -> None:
    """唯一引用 → 删 media + 删 bytes。"""
    await objectstore.put("media/only.jpg", b"data", None)
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/only.jpg")]))
    await app.delete_media(100, 1, 0)
    # bytes 真删
    assert not await objectstore.exists("media/only.jpg")


@pytest.mark.asyncio
async def test_delete_media_publishes_media_deleted_event(
    app: AppService, storage: StorageRepository, bus: EventBus,
) -> None:
    """delete_media 触发 MediaDeleted 事件 — UI 据此刷新 LIVE 流。"""
    received: list[MediaDeleted] = []
    bus.subscribe(MediaDeleted, lambda e: received.append(e))  # type: ignore[arg-type]

    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    await app.delete_media(100, 1, 0)
    assert len(received) == 1
    assert received[0].channel_id == 100
    assert received[0].telegram_msg_id == 1
    assert received[0].media_idx == 0


# ---- retry_media -----------------------------------------------------


@pytest.mark.asyncio
async def test_retry_failed_resets_to_pending_and_redownloads(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
    monkeypatch,
) -> None:
    """FAILED media 被 retry:状态重置 → PENDING → 走 download_one(force=True)。

    注:`app.downloader` 在 conftest fixture 里可能为 None(monitor 没注入)。
    这里手工注入一个 mock downloader 来验证 retry 路径。
    """
    download_calls: list[tuple] = []

    class _StubDownloader:
        async def download_one(self, *, msg_pk, media, force=False):  # noqa: ARG002
            download_calls.append((msg_pk, media.telegram_file_id, force))
            return dataclasses.replace(
                media,
                object_key=f"media/{media.telegram_file_id}.bin",
                object_backend="local",
                download_status=MediaDownloadStatus.DONE,
            )

    app.downloader = _StubDownloader()  # type: ignore[assignment]

    await storage.save_message(_msg(100, 1, [_failed_media("f1")]))
    await app.retry_media(100, 1, 0)

    # 走 force=True
    assert download_calls and download_calls[0][2] is True
    # 落库后状态变 DONE
    m = await storage.get_message(100, 1)
    assert m is not None
    assert m.media[0].download_status == MediaDownloadStatus.DONE


@pytest.mark.asyncio
async def test_retry_skips_non_failed(
    app: AppService, storage: StorageRepository,
) -> None:
    """非 FAILED 的 media 调 retry → no-op(不发事件,不改状态)。"""
    received: list[MediaRetried] = []
    app.bus.subscribe(MediaRetried, lambda e: received.append(e))  # type: ignore[arg-type]
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    await app.retry_media(100, 1, 0)
    # 非 FAILED → 跳过
    assert received == []
    m = await storage.get_message(100, 1)
    assert m is not None
    assert m.media[0].download_status == MediaDownloadStatus.DONE


@pytest.mark.asyncio
async def test_retry_publishes_retried_and_downloaded_events(
    app: AppService, storage: StorageRepository,
) -> None:
    """retry 成功 → 同时发 MediaRetried + MediaDownloaded 两个事件。"""
    retried: list[MediaRetried] = []
    downloaded: list[MediaDownloaded] = []
    app.bus.subscribe(MediaRetried, lambda e: retried.append(e))  # type: ignore[arg-type]
    app.bus.subscribe(MediaDownloaded, lambda e: downloaded.append(e))  # type: ignore[arg-type]

    class _StubDownloader:
        async def download_one(self, *, msg_pk, media, force=False):  # noqa: ARG002
            return dataclasses.replace(
                media,
                object_key=f"media/{media.telegram_file_id}.bin",
                object_backend="local",
                download_status=MediaDownloadStatus.DONE,
            )

    app.downloader = _StubDownloader()  # type: ignore[assignment]
    await storage.save_message(_msg(100, 1, [_failed_media("f1")]))
    await app.retry_media(100, 1, 0)
    assert len(retried) == 1
    assert len(downloaded) == 1


# ---- open_media ------------------------------------------------------


@pytest.mark.asyncio
async def test_open_media_returns_false_for_failed(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
) -> None:
    """FAILED media 返 False(QDesktopServices 不能打开不存在的文件)。"""
    await storage.save_message(_msg(100, 1, [_failed_media("f1")]))
    ok = await app.open_media(100, 1, 0)
    assert ok is False


@pytest.mark.asyncio
async def test_open_media_returns_false_for_missing_message(
    app: AppService,
) -> None:
    """message 不存在 → False。"""
    ok = await app.open_media(999, 1, 0)
    assert ok is False


@pytest.mark.asyncio
async def test_open_media_local_done_returns_bool(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
) -> None:
    """DONE + Local 后端 → 调 QDesktopServices.openUrl(成功与否依赖环境,只验不抛)。

    CI offscreen 环境 QDesktopServices.openUrl 可能返 False 但不抛;这里只看
    不抛异常 + 返 bool 类型。
    """
    await objectstore.put("media/photo.jpg", b"jpeg-bytes", None)
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/photo.jpg")]))
    ok = await app.open_media(100, 1, 0)
    assert isinstance(ok, bool)


@pytest.mark.asyncio
async def test_open_media_folder_done_returns_bool(
    app: AppService, storage: StorageRepository, tmp_path: Path,
) -> None:
    """FolderObjectStore 后端也走 openUrl,验证 bool 返值。"""
    folder = FolderObjectStore(root=tmp_path / "folder_media")
    await folder.connect()
    await folder.put("media/photo.jpg", b"jpeg", None)
    # 临时把 objects 切到 folder
    saved = app.objects
    app.objects = folder  # type: ignore[assignment]
    try:
        await storage.save_message(_msg(100, 1, [_done_media("f1", "media/photo.jpg")]))
        ok = await app.open_media(100, 1, 0)
        assert isinstance(ok, bool)
    finally:
        app.objects = saved  # type: ignore[assignment]


# ---- reconcile_orphans:测试放 tests/test_orphan_reconcile.py,这里只验事件发布 ----


@pytest.mark.asyncio
async def test_reconcile_orphans_emits_event(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
    bus: EventBus,
) -> None:
    """reconcile 跑完 → 发 MediaReconcileFinished 事件 + 返 evt。"""
    received: list[MediaReconcileFinished] = []
    bus.subscribe(MediaReconcileFinished, lambda e: received.append(e))  # type: ignore[arg-type]
    # ObjectStore 里有个孤儿 key
    await objectstore.put("media/orphan.jpg", b"orphan", None)
    evt = await app.reconcile_orphans(dry_run=True)
    assert isinstance(evt, MediaReconcileFinished)
    assert evt.backend == "local"
    assert evt.scanned >= 1
    assert evt.orphans >= 1
    assert evt.dry_run is True
    # dry_run 不真删
    assert await objectstore.exists("media/orphan.jpg")
    assert len(received) == 1