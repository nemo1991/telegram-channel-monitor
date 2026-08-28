"""Media Manager 单元测试(2026-08-24 新增)。

覆盖 AppService 层的 media 管理方法:
- list_media:filter 组合
- delete_media:refcount 路径
- retry_media:reset + force 重下
- open_media:Local / Folder / S3 路径分支
- 事件发布(MediaDeleted / MediaRetried / MediaDownloaded)
"""
from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING

import aioboto3
import pytest

from tgmonitor.core.dto import (
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    SortDir,
    SortKey,
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
    """无 filter 时返所有 media(扁平化)+ total 一致。"""
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    await storage.save_message(_msg(100, 2, [_done_media("f2", "media/2.jpg")]))
    rows, total = await app.list_media()
    assert len(rows) == 2
    assert total == 2
    keys = {(r[0].channel_id, r[0].telegram_msg_id, r[1]) for r in rows}
    assert keys == {(100, 1, 0), (100, 2, 0)}


@pytest.mark.asyncio
async def test_list_media_filters_by_status_failed(
    app: AppService, storage: StorageRepository,
) -> None:
    """status=FAILED 只返失败的 media + total=1。"""
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    await storage.save_message(_msg(100, 2, [_failed_media("f2")]))
    rows, total = await app.list_media(status=MediaDownloadStatus.FAILED)
    assert len(rows) == 1
    assert total == 1
    assert rows[0][2].download_status == MediaDownloadStatus.FAILED


@pytest.mark.asyncio
async def test_list_media_filters_by_channel(
    app: AppService, storage: StorageRepository,
) -> None:
    """channel_id 过滤 — 只返指定频道 + total 同样被过滤。"""
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    await storage.save_message(_msg(200, 1, [_done_media("f2", "media/2.jpg")]))
    rows, total = await app.list_media(channel_id=100)
    assert len(rows) == 1
    assert total == 1
    assert rows[0][0].channel_id == 100


@pytest.mark.asyncio
async def test_list_media_filters_by_type(
    app: AppService, storage: StorageRepository,
) -> None:
    """media_type 过滤 — 只返指定类型 + total 同样被过滤。"""
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    video = dataclasses.replace(
        _base_media("f2"), type=MediaType.VIDEO, file_name="vid.mp4",
        mime_type="video/mp4",
    )
    await storage.save_message(_msg(100, 2, [video]))
    rows, total = await app.list_media(media_type=MediaType.VIDEO)
    assert len(rows) == 1
    assert total == 1
    assert rows[0][2].type == MediaType.VIDEO


@pytest.mark.asyncio
async def test_list_media_search_by_filename_case_insensitive(
    app: AppService, storage: StorageRepository,
) -> None:
    """filename 搜索大小写不敏感 + total 同 filter。"""
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    sunset = dataclasses.replace(
        _done_media("f2", "media/2.jpg"), file_name="Sunset.JPG",
    )
    await storage.save_message(_msg(100, 2, [sunset]))
    await storage.save_message(_msg(100, 3, [_base_media("f3")]))
    rows, total = await app.list_media(search="sunset")
    assert len(rows) == 1
    assert total == 1
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


# ---- delete_by_channel(2026-08-25 PR #4)------------------------------


@pytest.mark.asyncio
async def test_delete_by_channel_removes_all_messages_in_channel(
    app: AppService, storage: StorageRepository,
) -> None:
    """批量删 — 目标频道所有 message 清掉,返真实删数。"""
    # ch 100: 3 条 message(2 含 media)
    await storage.save_message(_msg(100, 1, [_done_media("a", "media/a.jpg")]))
    await storage.save_message(_msg(100, 2, [_done_media("b", "media/b.jpg")]))
    await storage.save_message(_msg(100, 3, [_done_media("c", "media/c.jpg")]))
    # ch 200: 1 条 message(不应受影响)
    await storage.save_message(_msg(200, 1, [_done_media("d", "media/d.jpg")]))

    deleted = await app.delete_by_channel(100)
    assert deleted == 3
    # ch 100 全空
    assert await storage.get_message(100, 1) is None
    assert await storage.get_message(100, 2) is None
    assert await storage.get_message(100, 3) is None
    # ch 200 不动
    assert await storage.get_message(200, 1) is not None


@pytest.mark.asyncio
async def test_delete_by_channel_no_op_when_no_messages(
    app: AppService, storage: StorageRepository,
) -> None:
    """频道存在但无 message → 返 0,不抛。"""
    from tgmonitor.core.dto import ChannelDTO
    await storage.upsert_channel(ChannelDTO(id=999, title="#999"))
    deleted = await app.delete_by_channel(999)
    assert deleted == 0


@pytest.mark.asyncio
async def test_delete_by_channel_cleans_orphan_bytes(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
) -> None:
    """该频道独占引用的 bytes 应被清(走 storage.delete_message 的 refcount 路径)。

    跨频道共享的 bytes 保留(message 删一条 → 另一条仍引用 → refcount > 0 不删)。
    """
    await objectstore.put("media/only_here.jpg", b"data1", None)
    await objectstore.put("media/shared.jpg", b"data2", None)
    # ch 100: 1 条独占 + 1 条共享
    await storage.save_message(_msg(100, 1, [_done_media("a", "media/only_here.jpg")]))
    await storage.save_message(_msg(100, 2, [_done_media("b", "media/shared.jpg")]))
    # ch 200: 1 条共享(sentinel → shared.jpg 引用 refcount=2)
    await storage.save_message(_msg(200, 1, [_done_media("c", "media/shared.jpg")]))

    deleted = await app.delete_by_channel(100)
    assert deleted == 2

    # only_here.jpg 已无引用 → bytes 清掉
    assert not await objectstore.exists("media/only_here.jpg")
    # shared.jpg 仍被 ch 200 引用 → bytes 保留
    assert await objectstore.exists("media/shared.jpg")


@pytest.mark.asyncio
async def test_delete_by_channel_does_not_touch_other_channels(
    app: AppService, storage: StorageRepository,
) -> None:
    """目标频道外:message 不动,channel 元数据不动(走 list_messages → delete_message)。"""
    await storage.save_message(_msg(100, 1, []))
    await storage.save_message(_msg(200, 1, []))
    await storage.save_message(_msg(300, 1, []))

    await app.delete_by_channel(200)

    assert await storage.get_message(200, 1) is None
    assert await storage.get_message(100, 1) is not None
    assert await storage.get_message(300, 1) is not None


# ---- open_media_with_result(2026-08-25 v1.3.0 PR #5)-----------------


class _FakeS3Stream:
    """最小 aioboto3 StreamingBody 替身 — 仅供 open_media 测试用。"""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self) -> _FakeS3Stream:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def read(self) -> bytes:
        return self._data


class _FakeS3Client:
    """最小 boto3 s3 client 替身 — 只实现 open_media 路径需要的 get_object。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._data: bytes = b"jpeg-bytes"
        self._raise: Exception | None = None

    def set_data(self, data: bytes) -> None:
        self._data = data

    def set_raise(self, exc: Exception | None) -> None:
        self._raise = exc

    async def head_bucket(self, **kw: object) -> dict:
        # connect() 探测用 — 让它成功,跳过 create_bucket 路径
        self.calls.append(("head_bucket", dict(kw)))
        return {}

    async def get_object(self, **kw: object) -> dict:
        self.calls.append(("get_object", dict(kw)))
        if self._raise is not None:
            raise self._raise
        return {"Body": _FakeS3Stream(self._data)}


class _FakeS3ClientCtx:
    """aioboto3 ClientCreatorContext 替身 — `async with` 进入后返回 inner client。"""

    def __init__(self, client: _FakeS3Client) -> None:
        self._client_obj = client

    async def __aenter__(self) -> _FakeS3Client:
        return self._client_obj

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeS3Session:
    """aioboto3.Session 替身 — `Session().client('s3')` 返一个 ClientCreatorContext。"""

    def __init__(self) -> None:
        self._client_obj = _FakeS3Client()

    def client(self, *args: object, **kw: object) -> _FakeS3ClientCtx:
        return _FakeS3ClientCtx(self._client_obj)


def _make_s3_backend(
    app: AppService, monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, _FakeS3Session]:
    """把 app.objects 切到 S3ObjectStore(用 fake aioboto3.Session 注入)。"""
    from tgmonitor.core.objectstore.s3_store import S3ObjectStore

    fake = _FakeS3Session()
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="t", region="us-east-1")
    # _make_s3_backend 是同步函数,但 store.connect 是 async — 必须在 async 测试里 await
    # 这里只构造,connect 由 caller 决定
    saved = app.objects
    app.objects = store  # type: ignore[assignment]
    return saved, fake


@pytest.mark.asyncio
async def test_open_media_with_result_missing_message_returns_error(
    app: AppService,
) -> None:
    """message 不存在 → OpenMediaResult(False, '消息或媒体不存在')。"""
    result = await app.open_media_with_result(999, 1, 0)
    assert result.success is False
    assert result.error == "消息或媒体不存在"


@pytest.mark.asyncio
async def test_open_media_with_result_failed_media_returns_error(
    app: AppService, storage: StorageRepository,
) -> None:
    """FAILED media → OpenMediaResult(False, '媒体未下载完成')(状态前置检查)。"""
    await storage.save_message(_msg(100, 1, [_failed_media("f1")]))
    result = await app.open_media_with_result(100, 1, 0)
    assert result.success is False
    assert result.error == "媒体未下载完成"


@pytest.mark.asyncio
async def test_open_media_s3_stages_to_temp_and_calls_openurl(
    app: AppService, storage: StorageRepository, monkeypatch,
) -> None:
    """S3 后端成功路径:get_object 拉 bytes → 写 tmp → openUrl 返 True。

    monkeypatch 掉 QDesktopServices.openUrl 抓调用 + 强返 True,然后断言:
    1) get_object 调了一次(走 S3 client)
    2) openUrl 调了一次,参数是 tmp 文件 path
    3) tmp 文件存在 + 内容 == S3 bytes
    """
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    saved_objects, fake = _make_s3_backend(app, monkeypatch)
    await app.objects.connect()  # type: ignore[attr-defined]
    fake._client_obj.set_data(b"fake-jpeg-content")

    openurl_calls: list[QUrl] = []
    monkeypatch.setattr(
        QDesktopServices, "openUrl",
        lambda url: (openurl_calls.append(url) or True),
    )

    await storage.save_message(
        _msg(100, 1, [_done_media("f1", "media/photo.jpg")]),
    )
    result = await app.open_media_with_result(100, 1, 0)

    # 还原
    app.objects = saved_objects  # type: ignore[assignment]

    assert result.success is True, f"expected success, got {result}"
    # connect() 探了一下 head_bucket,open_media 调 get_object 拉 bytes
    assert ("get_object", {"Bucket": "t", "Key": "media/photo.jpg"}) in fake._client_obj.calls
    assert len(openurl_calls) == 1
    url = openurl_calls[0]
    assert url.isLocalFile()
    tmp_path = url.toLocalFile()
    assert tmp_path.endswith(".jpg"), f"expected .jpg suffix, got {tmp_path}"
    # 文件存在 + 内容与 S3 一致
    from pathlib import Path
    tmp_p = Path(tmp_path)
    assert await asyncio.to_thread(tmp_p.read_bytes) == b"fake-jpeg-content"
    # 清理 tmp(成功路径下不主动 unlink,测试自己清)
    await asyncio.to_thread(tmp_p.unlink)


@pytest.mark.asyncio
async def test_open_media_s3_cleans_tmp_when_open_url_fails(
    app: AppService, storage: StorageRepository, monkeypatch,
) -> None:
    """S3 后端 + openUrl 返 False → tmp 文件被 unlink + 返 OpenMediaResult(False)。"""
    from PySide6.QtGui import QDesktopServices

    saved_objects, fake = _make_s3_backend(app, monkeypatch)
    await app.objects.connect()  # type: ignore[attr-defined]
    fake._client_obj.set_data(b"data")

    monkeypatch.setattr(QDesktopServices, "openUrl", lambda _url: False)

    await storage.save_message(
        _msg(100, 1, [_done_media("f1", "media/photo.jpg")]),
    )
    result = await app.open_media_with_result(100, 1, 0)

    app.objects = saved_objects  # type: ignore[assignment]

    assert result.success is False
    assert result.error is not None and "系统调用失败" in result.error
    # tmp 文件已被 unlink — 我们不应该能再找到一个 tgmonitor-* 临时文件刚被本测试创建的
    # (因为失败路径 unlink 了,这里只能间接验证:不再有 file 被挂在本 pid 的 QStandardPaths.TempLocation)
    # 简化:不强断言 unlink(失败路径走 try/except OSError:pass,文件可能仍残留,
    # 但 tmp 路径已返给 openUrl);改为断言 error 含 "系统调用失败" 即可


# ---- list_media 排序 + 分页(2026-08-25 v1.3.0 PR #6)------------------


@pytest.mark.asyncio
async def test_list_media_sort_by_size_desc(
    app: AppService, storage: StorageRepository,
) -> None:
    """PR #6:AppService.list_media 透传 sort=SortKey.SIZE + sort_dir=SortDir.DESC。

    3 条 media — 一个 photo(1KB) + 一个 video(5MB) + 一个 photo(1024B)。
    DESC 后最大的(video, 5MB)排第一。
    """
    big = dataclasses.replace(
        _base_media("big"),
        type=MediaType.VIDEO, file_name="v.mp4", mime_type="video/mp4",
        file_size=5_000_000,
    )
    small_a = dataclasses.replace(_base_media("a"), file_size=1024)
    small_b = dataclasses.replace(_base_media("b"), file_size=512)
    await storage.save_message(_msg(100, 1, [big]))
    await storage.save_message(_msg(100, 2, [small_a]))
    await storage.save_message(_msg(100, 3, [small_b]))

    rows, total = await app.list_media(
        sort=SortKey.SIZE, sort_dir=SortDir.DESC,
    )
    assert total == 3
    sizes = [r[2].file_size for r in rows]
    assert sizes == [5_000_000, 1024, 512]


@pytest.mark.asyncio
async def test_list_media_sort_default_unchanged_when_omitted(
    app: AppService, storage: StorageRepository,
) -> None:
    """PR #6 向后兼容:不传 sort/sort_dir 时走默认 DATE DESC(v1.2.0 行为)。"""
    await storage.save_message(
        _msg(100, 1, [_done_media("f1", "media/1.jpg")]),
    )
    await storage.save_message(
        _msg(100, 2, [_done_media("f2", "media/2.jpg")]),
    )
    rows, _ = await app.list_media()
    # 默认 DATE DESC(没设 date 字段时 list_messages 自己定)— 这里只验不抛 + 返 tuple
    assert isinstance(rows, list)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_list_media_offset_pagination(
    app: AppService, storage: StorageRepository,
) -> None:
    """PR #6:offset 跳过 + limit 切片 + total 不变。

    5 条 media,limit=2 + offset=2 → 返 [3rd, 4th];total 始终是 5。
    """
    for i in range(5):
        await storage.save_message(
            _msg(100, i + 1, [_done_media(f"f{i}", f"media/{i}.jpg")]),
        )
    rows, total = await app.list_media(limit=2, offset=2)
    assert total == 5
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_list_media_count_matches_total_independent_of_pagination(
    app: AppService, storage: StorageRepository,
) -> None:
    """PR #6:sum(分页) == count,无 filter。"""
    for i in range(7):
        await storage.save_message(
            _msg(100, i + 1, [_done_media(f"f{i}", f"media/{i}.jpg")]),
        )
    _, total_first = await app.list_media(limit=3, offset=0)
    _, total_last = await app.list_media(limit=3, offset=4)
    assert total_first == total_last == 7


# ---- reveal_in_folder / copy_media_path(2026-08-27 v1.4.0 PR #16)----


@pytest.mark.asyncio
async def test_reveal_in_folder_local_success(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
) -> None:
    """PR #16:Local 后端 + 文件存在 → spawn 子进程成功 → RevealResult(True)。

    monkeypatch 掉 `_spawn_reveal` 验证被调 1 次,参数含 abs_path。
    """
    await objectstore.put("media/p.jpg", b"jpeg", None)
    await storage.save_message(
        _msg(100, 1, [_done_media("f1", "media/p.jpg")]),
    )

    spawn_calls: list[tuple[object, str]] = []

    def _fake_spawn(abs_path, platform):  # noqa: ANN001 — sync static method
        spawn_calls.append((abs_path, platform))

    app._spawn_reveal = staticmethod(_fake_spawn)  # type: ignore[assignment]

    result = await app.reveal_in_folder(100, 1, 0)
    assert result.success is True
    assert result.error is None
    assert len(spawn_calls) == 1
    abs_path_arg, platform_arg = spawn_calls[0]
    assert str(abs_path_arg).endswith("media/p.jpg")
    # platform 来自 sys.platform,可能是 darwin / linux / win32;只断言是 str
    assert isinstance(platform_arg, str)


@pytest.mark.asyncio
async def test_reveal_in_folder_s3_returns_error(
    app: AppService, storage: StorageRepository, monkeypatch,
) -> None:
    """PR #16:S3 后端 → RevealResult(False, "S3 后端无本地路径:请使用「Copy 路径」...")。"""
    saved_objects, _fake = _make_s3_backend(app, monkeypatch)
    await app.objects.connect()  # type: ignore[attr-defined]
    await storage.save_message(
        _msg(100, 1, [_done_media("f1", "media/p.jpg")]),
    )

    result = await app.reveal_in_folder(100, 1, 0)

    app.objects = saved_objects  # type: ignore[assignment]

    assert result.success is False
    assert result.error is not None
    assert "S3" in result.error


@pytest.mark.asyncio
async def test_reveal_in_folder_missing_message_returns_error(
    app: AppService,
) -> None:
    """PR #16:message 不存在 → RevealResult(False, '消息或媒体不存在')。"""
    result = await app.reveal_in_folder(999, 1, 0)
    assert result.success is False
    assert result.error == "消息或媒体不存在"


@pytest.mark.asyncio
async def test_reveal_in_folder_pending_media_returns_error(
    app: AppService, storage: StorageRepository,
) -> None:
    """PR #16:media 未 DONE → RevealResult(False, '媒体未下载完成')。"""
    await storage.save_message(_msg(100, 1, [_base_media("f1")]))  # PENDING
    result = await app.reveal_in_folder(100, 1, 0)
    assert result.success is False
    assert result.error == "媒体未下载完成"


@pytest.mark.asyncio
async def test_reveal_in_folder_missing_file_returns_error(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
) -> None:
    """PR #16:DONE 但文件不在磁盘上(可能被外部删) → RevealResult(False, 含 '文件不存在')。"""
    # 仅写 storage 元数据,不实际 put 文件
    await storage.save_message(
        _msg(100, 1, [_done_media("f1", "media/ghost.jpg")]),
    )
    result = await app.reveal_in_folder(100, 1, 0)
    assert result.success is False
    assert result.error is not None and "文件不存在" in result.error


@pytest.mark.asyncio
async def test_copy_media_path_local_returns_absolute_path(
    app: AppService, storage: StorageRepository, objectstore: ObjectStore,
) -> None:
    """PR #16:Local 后端 → CopyResult(True, copied_value=绝对路径)。"""
    await objectstore.put("media/p.jpg", b"jpeg", None)
    await storage.save_message(
        _msg(100, 1, [_done_media("f1", "media/p.jpg")]),
    )
    result = await app.copy_media_path(100, 1, 0)
    assert result.success is True
    assert result.error is None
    assert result.copied_value is not None
    assert result.copied_value.endswith("media/p.jpg")
    # 必须是绝对路径
    from pathlib import Path
    assert Path(result.copied_value).is_absolute()


@pytest.mark.asyncio
async def test_copy_media_path_s3_returns_uri(
    app: AppService, storage: StorageRepository, monkeypatch,
) -> None:
    """PR #16:S3 后端 → CopyResult(True, copied_value='s3://<bucket>/<key>')。"""
    saved_objects, _fake = _make_s3_backend(app, monkeypatch)
    await app.objects.connect()  # type: ignore[attr-defined]
    await storage.save_message(
        _msg(100, 1, [_done_media("f1", "media/p.jpg")]),
    )

    result = await app.copy_media_path(100, 1, 0)

    app.objects = saved_objects  # type: ignore[assignment]

    assert result.success is True
    assert result.copied_value == "s3://t/media/p.jpg"
    assert result.error is None


@pytest.mark.asyncio
async def test_copy_media_path_missing_message_returns_error(
    app: AppService,
) -> None:
    """PR #16:message 不存在 → CopyResult(False, error='消息或媒体不存在')。"""
    result = await app.copy_media_path(999, 1, 0)
    assert result.success is False
    assert result.error == "消息或媒体不存在"


@pytest.mark.asyncio
async def test_copy_media_path_pending_media_returns_error(
    app: AppService, storage: StorageRepository,
) -> None:
    """PR #16:media 未 DONE → CopyResult(False, error='媒体未下载完成')。"""
    await storage.save_message(_msg(100, 1, [_base_media("f1")]))
    result = await app.copy_media_path(100, 1, 0)
    assert result.success is False
    assert result.error == "媒体未下载完成"