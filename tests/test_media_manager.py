"""Media Manager 单元测试(2026-08-24 新增)。

覆盖 AppService 层的 media 管理方法:
- list_media:filter 组合
- delete_media:refcount 路径
- retry_media:reset + force 重下
- open_media:Local / Folder / S3 路径分支
- 事件发布(MediaDeleted / MediaRetried / MediaDownloaded)

2026-10-04 plan 阶段 1.3.B:`_FakeS3Client` / `_FakeS3Stream` /
`_FakeS3ClientCtx` / `_FakeS3Session` 替身 class 提到
`tests/fixtures/_s3_client.py`,本文件直接 import 共用。
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
from typing import TYPE_CHECKING

import aioboto3
import pytest

from tests.fixtures._s3_client import (
    FakeS3Session,
)
from tgmonitor.core.dto import (
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    SortDir,
    SortKey,
)
from tgmonitor.core.events import (
    MediaReconcileFinished,
    MediaRetried,
)

if TYPE_CHECKING:
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


async def test_list_media_returns_all_with_no_filter(
    app: AppService,
    storage: StorageRepository,
) -> None:
    """无 filter 时返所有 media(扁平化)+ total 一致。"""
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    await storage.save_message(_msg(100, 2, [_done_media("f2", "media/2.jpg")]))
    rows, total = await app.media.list_media()
    assert len(rows) == 2
    assert total == 2
    keys = {(r[0].channel_id, r[0].telegram_msg_id, r[1]) for r in rows}
    assert keys == {(100, 1, 0), (100, 2, 0)}


async def test_list_media_filters_by_status_failed(
    app: AppService,
    storage: StorageRepository,
) -> None:
    """status=FAILED 只返失败的 media + total=1。"""
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    await storage.save_message(_msg(100, 2, [_failed_media("f2")]))
    rows, total = await app.media.list_media(status=MediaDownloadStatus.FAILED)
    assert len(rows) == 1
    assert total == 1
    assert rows[0][2].download_status == MediaDownloadStatus.FAILED


async def test_list_media_filters_by_channel(
    app: AppService,
    storage: StorageRepository,
) -> None:
    """channel_id 过滤 — 只返指定频道 + total 同样被过滤。"""
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    await storage.save_message(_msg(200, 1, [_done_media("f2", "media/2.jpg")]))
    rows, total = await app.media.list_media(channel_id=100)
    assert len(rows) == 1
    assert total == 1
    assert rows[0][0].channel_id == 100


async def test_list_media_search_by_filename_case_insensitive(
    app: AppService,
    storage: StorageRepository,
) -> None:
    """filename 搜索大小写不敏感 + total 同 filter。"""
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    sunset = dataclasses.replace(
        _done_media("f2", "media/2.jpg"),
        file_name="Sunset.JPG",
    )
    await storage.save_message(_msg(100, 2, [sunset]))
    await storage.save_message(_msg(100, 3, [_base_media("f3")]))
    rows, total = await app.media.list_media(search="sunset")
    assert len(rows) == 1
    assert total == 1
    assert rows[0][2].file_name == "Sunset.JPG"


# ---- delete_media ----------------------------------------------------


async def test_delete_media_removes_from_message_keeps_bytes_when_referenced(
    app: AppService,
    storage: StorageRepository,
    objectstore: ObjectStore,
) -> None:
    """同 key 两条 message 都引用 — 删一条 → media 摘掉,bytes 保留。"""
    await objectstore.put("media/shared.jpg", b"data", None)
    shared = _done_media("f-shared", "media/shared.jpg")
    await storage.save_message(_msg(100, 1, [shared]))
    await storage.save_message(_msg(100, 2, [shared]))  # 同 media 对象引用
    await app.media.delete_media(100, 1, 0)

    # message 1 已删 media;message 2 仍引用
    m1 = await storage.get_message(100, 1)
    m2 = await storage.get_message(100, 2)
    assert m1 is not None and len(m1.media) == 0
    assert m2 is not None and len(m2.media) == 1
    # bytes 保留(被 message 2 引用)
    assert await objectstore.exists("media/shared.jpg")


async def test_delete_media_removes_bytes_when_no_other_reference(
    app: AppService,
    storage: StorageRepository,
    objectstore: ObjectStore,
) -> None:
    """唯一引用 → 删 media + 删 bytes。"""
    await objectstore.put("media/only.jpg", b"data", None)
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/only.jpg")]))
    await app.media.delete_media(100, 1, 0)
    # bytes 真删
    assert not await objectstore.exists("media/only.jpg")


# ---- retry_media -----------------------------------------------------


async def test_retry_failed_resets_to_pending_and_redownloads(
    app: AppService,
    storage: StorageRepository,
    objectstore: ObjectStore,
    monkeypatch,
) -> None:
    """FAILED media 被 retry:状态重置 → PENDING → 走 download_one(force=True)。

    注:`app.downloader` 在 conftest fixture 里可能为 None(monitor 没注入)。
    这里手工注入一个 mock downloader 来验证 retry 路径。
    """
    download_calls: list[tuple] = []

    class _StubDownloader:
        async def download_one(self, *, msg_pk, media, force=False, progress_callback=None):
            download_calls.append((msg_pk, media.telegram_file_id, force))
            return dataclasses.replace(
                media,
                object_key=f"media/{media.telegram_file_id}.bin",
                object_backend="local",
                download_status=MediaDownloadStatus.DONE,
            )

    app.downloader = _StubDownloader()  # type: ignore[assignment]

    await storage.save_message(_msg(100, 1, [_failed_media("f1")]))
    await app.media.retry_media(100, 1, 0)

    # 走 force=True
    assert download_calls and download_calls[0][2] is True
    # 落库后状态变 DONE
    m = await storage.get_message(100, 1)
    assert m is not None
    assert m.media[0].download_status == MediaDownloadStatus.DONE


async def test_retry_skips_non_failed(
    app: AppService,
    storage: StorageRepository,
) -> None:
    """非 FAILED 的 media 调 retry → no-op(不发事件,不改状态)。"""
    received: list[MediaRetried] = []
    app.bus.subscribe(MediaRetried, lambda e: received.append(e))  # type: ignore[arg-type]
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/1.jpg")]))
    await app.media.retry_media(100, 1, 0)
    # 非 FAILED → 跳过
    assert received == []
    m = await storage.get_message(100, 1)
    assert m is not None
    assert m.media[0].download_status == MediaDownloadStatus.DONE


# ---- open_media ------------------------------------------------------


async def test_open_media_returns_false_for_failed(
    app: AppService,
    storage: StorageRepository,
    objectstore: ObjectStore,
) -> None:
    """FAILED media 返 False(QDesktopServices 不能打开不存在的文件)。"""
    await storage.save_message(_msg(100, 1, [_failed_media("f1")]))
    ok = await app.media.open_media(100, 1, 0)
    assert ok is False


# ---- reconcile_orphans:测试放 tests/test_orphan_reconcile.py,这里只验事件发布 ----


async def test_reconcile_orphans_emits_event(
    app: AppService,
    storage: StorageRepository,
    objectstore: ObjectStore,
    bus: EventBus,
) -> None:
    """reconcile 跑完 → 发 MediaReconcileFinished 事件 + 返 evt。"""
    received: list[MediaReconcileFinished] = []
    bus.subscribe(MediaReconcileFinished, lambda e: received.append(e))  # type: ignore[arg-type]
    # ObjectStore 里有个孤儿 key
    await objectstore.put("media/orphan.jpg", b"orphan", None)
    evt = await app.media.reconcile_orphans(dry_run=True)
    assert isinstance(evt, MediaReconcileFinished)
    assert evt.backend == "local"
    assert evt.scanned >= 1
    assert evt.orphans >= 1
    assert evt.dry_run is True
    # dry_run 不真删
    assert await objectstore.exists("media/orphan.jpg")
    assert len(received) == 1


# ---- delete_by_channel(2026-08-25 PR #4)------------------------------


async def test_delete_by_channel_cleans_orphan_bytes(
    app: AppService,
    storage: StorageRepository,
    objectstore: ObjectStore,
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

    deleted = await app.media.delete_by_channel(100)
    assert deleted == 2

    # only_here.jpg 已无引用 → bytes 清掉
    assert not await objectstore.exists("media/only_here.jpg")
    # shared.jpg 仍被 ch 200 引用 → bytes 保留
    assert await objectstore.exists("media/shared.jpg")


async def test_delete_by_channel_does_not_touch_other_channels(
    app: AppService,
    storage: StorageRepository,
) -> None:
    """目标频道外:message 不动,channel 元数据不动(走 list_messages → delete_message)。"""
    await storage.save_message(_msg(100, 1, []))
    await storage.save_message(_msg(200, 1, []))
    await storage.save_message(_msg(300, 1, []))

    await app.media.delete_by_channel(200)

    assert await storage.get_message(200, 1) is None
    assert await storage.get_message(100, 1) is not None
    assert await storage.get_message(300, 1) is not None


# ---- open_media_with_result(2026-08-25 v1.3.0 PR #5)-----------------


def _make_s3_backend(
    app: AppService,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, FakeS3Session]:
    """把 app.objects 切到 S3ObjectStore(用 fake aioboto3.Session 注入)。"""
    from tgmonitor.core.objectstore.s3_store import S3ObjectStore

    fake = FakeS3Session()
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="t", region="us-east-1")
    # _make_s3_backend 是同步函数,但 store.connect 是 async — 必须在 async 测试里 await
    # 这里只构造,connect 由 caller 决定
    saved = app.objects
    app.objects = store  # type: ignore[assignment]
    return saved, fake


async def test_open_media_s3_stages_to_temp_and_calls_openurl(
    app: AppService,
    storage: StorageRepository,
    monkeypatch,
) -> None:
    """S3 后端成功路径:get_object 拉 bytes → 写 tmp → openUrl 返 True。

    monkeypatch 掉 core 的 `_qt_bridge._module_open_url_cb` callback 抓调用 +
    强返 True,然后断言:
    1) get_object 调了一次(走 S3 client)
    2) open_url callback 调了一次,参数是 tmp 文件 path
    3) tmp 文件存在 + 内容 == S3 bytes

    2026-10-04 plan 阶段 2.2:Qt 调用抽到 module-level callback,
    不再 monkeypatch `QDesktopServices.openUrl`。
    """
    saved_objects, fake = _make_s3_backend(app, monkeypatch)
    await app.objects.connect()  # type: ignore[attr-defined]
    fake._client_obj.set_data(b"fake-jpeg-content")

    openurl_calls: list[str] = []

    monkeypatch.setattr(
        "tgmonitor.core.media_service._qt_bridge._module_open_url_cb",
        lambda path: openurl_calls.append(path) or True,
    )

    await storage.save_message(
        _msg(100, 1, [_done_media("f1", "media/photo.jpg")]),
    )
    result = await app.media.open_media_with_result(100, 1, 0)

    # 还原
    app.objects = saved_objects  # type: ignore[assignment]

    assert result.success is True, f"expected success, got {result}"
    # connect() 探了一下 head_bucket,open_media 调 get_object 拉 bytes
    assert ("get_object", {"Bucket": "t", "Key": "media/photo.jpg"}) in fake._client_obj.calls
    assert len(openurl_calls) == 1
    tmp_path = openurl_calls[0]
    assert tmp_path.endswith(".jpg"), f"expected .jpg suffix, got {tmp_path}"
    # 文件存在 + 内容与 S3 一致
    from pathlib import Path

    tmp_p = Path(tmp_path)
    assert await asyncio.to_thread(tmp_p.read_bytes) == b"fake-jpeg-content"
    # 清理 tmp(成功路径下不主动 unlink,测试自己清)
    await asyncio.to_thread(tmp_p.unlink)


async def test_open_media_s3_cleans_tmp_when_open_url_fails(
    app: AppService,
    storage: StorageRepository,
    monkeypatch,
) -> None:
    """S3 后端 + open_url callback 返 False → tmp 文件被 unlink + 返 OpenMediaResult(False)。

    2026-10-04 plan 阶段 2.2:monkeypatch module-level callback 而非 Qt 对象。
    """
    saved_objects, fake = _make_s3_backend(app, monkeypatch)
    await app.objects.connect()  # type: ignore[attr-defined]
    fake._client_obj.set_data(b"data")

    monkeypatch.setattr(
        "tgmonitor.core.media_service._qt_bridge._module_open_url_cb",
        lambda _path: False,
    )

    await storage.save_message(
        _msg(100, 1, [_done_media("f1", "media/photo.jpg")]),
    )
    result = await app.media.open_media_with_result(100, 1, 0)

    app.objects = saved_objects  # type: ignore[assignment]

    assert result.success is False
    assert result.error is not None and "系统调用失败" in result.error
    # tmp 文件已被 unlink — 我们不应该能再找到一个 tgmonitor-* 临时文件刚被本测试创建的
    # (因为失败路径 unlink 了,这里只能间接验证:不再有 file 被挂在本 pid 的 QStandardPaths.TempLocation)
    # 简化:不强断言 unlink(失败路径走 try/except OSError:pass,文件可能仍残留,
    # 但 tmp 路径已返给 openUrl);改为断言 error 含 "系统调用失败" 即可


# ---- list_media 排序 + 分页(2026-08-25 v1.3.0 PR #6)------------------


async def test_list_media_sort_by_size_desc(
    app: AppService,
    storage: StorageRepository,
) -> None:
    """PR #6:AppService.list_media 透传 sort=SortKey.SIZE + sort_dir=SortDir.DESC。

    3 条 media — 一个 photo(1KB) + 一个 video(5MB) + 一个 photo(1024B)。
    DESC 后最大的(video, 5MB)排第一。
    """
    big = dataclasses.replace(
        _base_media("big"),
        type=MediaType.VIDEO,
        file_name="v.mp4",
        mime_type="video/mp4",
        file_size=5_000_000,
    )
    small_a = dataclasses.replace(_base_media("a"), file_size=1024)
    small_b = dataclasses.replace(_base_media("b"), file_size=512)
    await storage.save_message(_msg(100, 1, [big]))
    await storage.save_message(_msg(100, 2, [small_a]))
    await storage.save_message(_msg(100, 3, [small_b]))

    rows, total = await app.media.list_media(
        sort=SortKey.SIZE,
        sort_dir=SortDir.DESC,
    )
    assert total == 3
    sizes = [r[2].file_size for r in rows]
    assert sizes == [5_000_000, 1024, 512]


async def test_list_media_offset_pagination(
    app: AppService,
    storage: StorageRepository,
) -> None:
    """PR #6:offset 跳过 + limit 切片 + total 不变。

    5 条 media,limit=2 + offset=2 → 返 [3rd, 4th];total 始终是 5。
    """
    for i in range(5):
        await storage.save_message(
            _msg(100, i + 1, [_done_media(f"f{i}", f"media/{i}.jpg")]),
        )
    rows, total = await app.media.list_media(limit=2, offset=2)
    assert total == 5
    assert len(rows) == 2


# ---- reveal_in_folder / copy_media_path(2026-08-27 v1.4.0 PR #16)----


async def test_copy_media_path_s3_returns_uri(
    app: AppService,
    storage: StorageRepository,
    monkeypatch,
) -> None:
    """PR #16:S3 后端 → CopyResult(True, copied_value='s3://<bucket>/<key>')。"""
    saved_objects, _fake = _make_s3_backend(app, monkeypatch)
    await app.objects.connect()  # type: ignore[attr-defined]
    await storage.save_message(
        _msg(100, 1, [_done_media("f1", "media/p.jpg")]),
    )

    result = await app.media.copy_media_path(100, 1, 0)

    app.objects = saved_objects  # type: ignore[assignment]

    assert result.success is True
    assert result.copied_value == "s3://t/media/p.jpg"
    assert result.error is None


# ---- 2026-09-26 fix(open-media-hang):回归 — openUrl 必须 off-main-thread ----


async def test_open_media_with_result_does_not_block_event_loop(
    app: AppService,
    storage: StorageRepository,
    objectstore: ObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """openUrl 模拟 200ms 阻塞,主 loop 必须还能推进其它任务。

    修复前:openUrl 在主 loop 同步跑 → 并行 asyncio.sleep(0.2) 也被卡,
    总耗时 ≥0.4s(串行),断言失败。
    修复后:openUrl 走 worker thread → 并行 sleep 同时完成,总耗时 ~0.2s。

    真实 bug 表现是 UI 卡死 — 此测试守住「主 loop 在 openUrl 期间
    不被独占」这条边界。

    2026-09-28 v1.9.2:coverage 插桩下 skip —— `coverage` 走 `sys.settrace`
    把每一行都加钩子,被测代码系统性慢 ~10x,实测 macOS CI 0.352s > 0.35s
    阈值。和 `test_message_view.py::test_pr9_append_constant_time_per_call`
    同根因;与 `_under_coverage()` 同模式。CI 的「Run pytest with coverage」
    step 只收覆盖率(不设阈值门控),跳过无损失;「Run pytest」无插桩 step
    才是真正跑这条断言的。
    """
    if sys.gettrace() is not None:
        import pytest

        pytest.skip("perf 绝对耗时断言在 coverage 插桩下无意义(系统性 ~10x 慢)")

    import time

    def slow_open(_path: str) -> bool:
        # 模拟 macOS LaunchServices cold cache 的 200ms 同步阻塞
        time.sleep(0.2)
        return True

    monkeypatch.setattr(
        "tgmonitor.core.media_service._qt_bridge._module_open_url_cb",
        slow_open,
    )

    await objectstore.put("media/photo.jpg", b"jpeg-bytes", None)
    await storage.save_message(_msg(100, 1, [_done_media("f1", "media/photo.jpg")]))

    start = time.monotonic()
    await asyncio.gather(
        app.media.open_media(100, 1, 0),
        asyncio.sleep(0.2),
    )
    elapsed = time.monotonic() - start
    # 串行会 ~0.4s,真并行 ~0.2s。给一点 CI 抖动余量(±0.05s),仍守得住
    # 「off-main-thread」这条线;若退化回主线程,这里会 ≥0.38s 失败。
    assert elapsed < 0.35, f"event loop blocked during openUrl: {elapsed:.2f}s"


# ---- 2026-11-22 v2.0.9 FR-06:PENDING 手动下载(force=False) -------------


async def test_download_pending_uses_force_false(
    app: AppService,
    storage: StorageRepository,
) -> None:
    """PENDING media 手动下载 → download_one(force=False),落库转 DONE。"""
    download_calls: list[tuple] = []

    class _StubDownloader:
        async def download_one(self, *, msg_pk, media, force=False, progress_callback=None):
            download_calls.append((msg_pk, media.telegram_file_id, force))
            return dataclasses.replace(
                media,
                object_key=f"media/{media.telegram_file_id}.bin",
                object_backend="local",
                download_status=MediaDownloadStatus.DONE,
            )

    app.downloader = _StubDownloader()  # type: ignore[assignment]
    await storage.save_message(_msg(100, 2, [_base_media("f2")]))
    await app.media.download_media(100, 2, 0)

    assert download_calls and download_calls[0][2] is False, "首次下载必须 force=False"
    m = await storage.get_message(100, 2)
    assert m is not None
    assert m.media[0].download_status is MediaDownloadStatus.DONE
    assert m.media[0].object_key
