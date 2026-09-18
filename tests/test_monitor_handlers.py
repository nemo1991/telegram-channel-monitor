"""2026-09-17 PR 5:MonitorService event handler 私方法直测。

PR 5 续 PR:覆盖 5 个 0-test 私方法 — `_handle_pin_changed` /
`_handle_channel_title_changed` / `_handle_channel_photo_changed` /
`_maybe_store_thumb` / `_download_worker`。

设计:
- 用 `AsyncMock(spec=StorageRepository)` 拦截 storage 调用,断言参数语义
- bus.subscribe 监听输出事件,断言事件 payload
- 不连真 TDLib / 真 storage,只验「事件 → storage call + event publish」链路
- 异常路径单独测:storage 抛 → handler 发 `ErrorOccurred` 不冒泡
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from tests.fixtures._async import wait_for
from tgmonitor.core.dto import (
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
)
from tgmonitor.core.events import (
    ChannelPhotoChanged,
    ChannelTitleChanged,
    ErrorOccurred,
    EventBus,
    MediaDownloaded,
    MediaDownloadProgress,
    MessageEdited,
    MessagePinChanged,
)
from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.fake_client import FakeTelegramClient


def _make_monitor_with_mocks() -> tuple[MonitorService, EventBus, MagicMock, MagicMock]:
    """构造最小 MonitorService + storage mock — 不连真存储。"""
    bus = EventBus()
    client = FakeTelegramClient()
    storage = MagicMock(spec=StorageRepository)
    storage.update_message_pin = AsyncMock(return_value=None)
    storage.update_channel_metadata = AsyncMock(return_value=None)
    storage.save_message = AsyncMock(return_value=None)
    storage.get_message = AsyncMock(return_value=None)
    objects = MagicMock()
    settings = MagicMock()
    mon = MonitorService(bus, client, storage, objects, settings)
    return mon, bus, storage, objects


# ============================================================
# `_handle_pin_changed` — MessagePinChanged 事件 → storage 落库 + republish
# ============================================================


async def test_handle_pin_changed_updates_storage_and_republishes() -> None:
    """pin 事件 → `storage.update_message_pin` 落库,re-fetch DTO + 发 `MessageEdited`."""
    mon, bus, storage, _ = _make_monitor_with_mocks()

    # mock get_message 返完整 DTO(republish 需要)
    updated_dto = MessageDTO(
        id=1,
        channel_id=100,
        telegram_msg_id=99,
        date=datetime(2026, 9, 17, tzinfo=UTC),
        text="x",
        is_pinned=True,
    )
    storage.get_message.return_value = updated_dto

    received: list[MessageEdited] = []
    bus.subscribe(MessageEdited, lambda e: received.append(e))

    await mon._handle_pin_changed(
        MessagePinChanged(channel_id=100, telegram_msg_id=99, is_pinned=True)
    )

    storage.update_message_pin.assert_awaited_once_with(100, 99, True)
    storage.get_message.assert_awaited_once_with(100, 99)
    assert len(received) == 1
    assert received[0].message is updated_dto  # 同一引用


async def test_handle_pin_changed_no_republish_if_dto_missing() -> None:
    """storage 找不到 message(陈年 chat)→ `get_message` 返 None,不 republish。"""
    mon, bus, storage, _ = _make_monitor_with_mocks()
    storage.get_message.return_value = None  # 模拟落库时机晚于 pin update

    received: list[MessageEdited] = []
    bus.subscribe(MessageEdited, lambda e: received.append(e))

    await mon._handle_pin_changed(
        MessagePinChanged(channel_id=100, telegram_msg_id=999, is_pinned=True)
    )

    storage.update_message_pin.assert_awaited_once_with(100, 999, True)
    storage.get_message.assert_awaited_once_with(100, 999)
    assert received == []  # 不发


async def test_handle_pin_changed_storage_error_publishes_error_event() -> None:
    """storage.update_message_pin 抛异常 → 不冒泡,改发 `ErrorOccurred` 事件。"""
    mon, bus, storage, _ = _make_monitor_with_mocks()
    storage.update_message_pin.side_effect = RuntimeError("db locked")

    errors: list[ErrorOccurred] = []
    bus.subscribe(ErrorOccurred, lambda e: errors.append(e))

    # 不应抛
    await mon._handle_pin_changed(
        MessagePinChanged(channel_id=100, telegram_msg_id=99, is_pinned=False)
    )

    assert len(errors) == 1
    assert errors[0].source == "monitor.pin"
    assert "db locked" in errors[0].message
    assert isinstance(errors[0].exception, RuntimeError)


# ============================================================
# `_handle_channel_title_changed`
# ============================================================


async def test_handle_channel_title_changed_updates_storage() -> None:
    """title 事件 → `storage.update_channel_metadata(channel_id, title=...)` 落库。"""
    mon, bus, storage, _ = _make_monitor_with_mocks()

    await mon._handle_channel_title_changed(
        ChannelTitleChanged(channel_id=100, new_title="新频道名")
    )

    storage.update_channel_metadata.assert_awaited_once_with(100, title="新频道名")


async def test_handle_channel_title_changed_storage_error_emits_error_event() -> None:
    """storage 抛 → handler 吞异常,改发 `ErrorOccurred(source='monitor.channel_title')`。"""
    mon, bus, storage, _ = _make_monitor_with_mocks()
    storage.update_channel_metadata.side_effect = RuntimeError("disk full")

    errors: list[ErrorOccurred] = []
    bus.subscribe(ErrorOccurred, lambda e: errors.append(e))

    await mon._handle_channel_title_changed(ChannelTitleChanged(channel_id=100, new_title="x"))

    assert len(errors) == 1
    assert errors[0].source == "monitor.channel_title"
    assert "disk full" in errors[0].message


# ============================================================
# `_handle_channel_photo_changed` — 三态语义
# ============================================================


async def test_handle_channel_photo_changed_with_path_writes_to_storage() -> None:
    """photo 非空 str → `storage.update_channel_metadata(photo_local_key=path)` 落库。"""
    mon, bus, storage, _ = _make_monitor_with_mocks()

    await mon._handle_channel_photo_changed(
        ChannelPhotoChanged(channel_id=100, local_path="/tmp/tdlib/photo_100.jpg")
    )

    storage.update_channel_metadata.assert_awaited_once_with(
        100, photo_local_key="/tmp/tdlib/photo_100.jpg"
    )


async def test_handle_channel_photo_changed_empty_string_writes_null() -> None:
    """photo='' (头像被删)→ 写真 NULL(`photo_local_key=None`)进 storage。

    三态:None = 不动 / '' = 删 / 非空 = 写真路径。
    """
    mon, bus, storage, _ = _make_monitor_with_mocks()

    await mon._handle_channel_photo_changed(ChannelPhotoChanged(channel_id=100, local_path=""))

    storage.update_channel_metadata.assert_awaited_once_with(100, photo_local_key=None)


async def test_handle_channel_photo_changed_none_is_no_op() -> None:
    """photo=None (不动字段)→ 不调 storage update_channel_metadata。"""
    mon, bus, storage, _ = _make_monitor_with_mocks()

    await mon._handle_channel_photo_changed(ChannelPhotoChanged(channel_id=100, local_path=None))

    storage.update_channel_metadata.assert_not_awaited()


async def test_handle_channel_photo_changed_storage_error_emits_error_event() -> None:
    """storage 抛 → 发 `ErrorOccurred(source='monitor.channel_photo')`,不冒泡。"""
    mon, bus, storage, _ = _make_monitor_with_mocks()
    storage.update_channel_metadata.side_effect = RuntimeError("i/o")

    errors: list[ErrorOccurred] = []
    bus.subscribe(ErrorOccurred, lambda e: errors.append(e))

    await mon._handle_channel_photo_changed(
        ChannelPhotoChanged(channel_id=100, local_path="/x.jpg")
    )

    assert len(errors) == 1
    assert errors[0].source == "monitor.channel_photo"


# ============================================================
# `_maybe_store_thumb` — 当前 no-op,锁定未来扩展点
# ============================================================


async def test_maybe_store_thumb_is_noop() -> None:
    """`_maybe_store_thumb(med)` 当前是 no-op — 锁住「不抛、不调 storage/objects」。

    未来扩展点(media 携带缩略图 bytes 字段时可在此入 ObjectStore)。
    """
    mon, bus, storage, objects = _make_monitor_with_mocks()
    med = MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name="x.jpg",
        file_size=1024,
        width=10,
        height=10,
        object_key="media/x.jpg",
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )

    # 不抛即可
    result = await mon._maybe_store_thumb(med)
    assert result is None
    # 不调 storage / objects
    storage.save_message.assert_not_awaited()
    storage.update_message_pin.assert_not_awaited()


# ============================================================
# `_download_worker` — 队列消费者:下载 → 回写 storage → 发 MediaDownloaded
# ============================================================


def _make_monitor_with_downloader() -> tuple[MonitorService, EventBus, MagicMock]:
    """构造带真 downloader 的 MonitorService — 用 mock downloader 替换。"""
    bus = EventBus()
    client = FakeTelegramClient()
    storage = MagicMock(spec=StorageRepository)
    storage.save_message = AsyncMock(return_value=None)
    objects = MagicMock()
    settings = MagicMock()
    mon = MonitorService(bus, client, storage, objects, settings)
    # mock downloader — `_download_worker` 需要
    downloader = MagicMock()
    downloader.download_one = AsyncMock(
        return_value=MediaDTO(
            type=MediaType.PHOTO,
            mime_type="image/jpeg",
            file_name="x.jpg",
            file_size=1024,
            width=10,
            height=10,
            object_key="media/x.jpg",
            object_backend="local",
            download_status=MediaDownloadStatus.DONE,
        )
    )
    mon.downloader = downloader
    # queue 由 `_start_download_worker_if_needed` 创建;手动建一个放进来
    mon._download_queue = asyncio.Queue()
    return mon, bus, storage


async def test_download_worker_processes_one_message_and_emits_event() -> None:
    """worker 拉一条消息 → `downloader.download_one` → `storage.save_message`
    回写 + 发 `MediaDownloaded` 事件。"""
    mon, bus, storage = _make_monitor_with_downloader()

    msg = MessageDTO(
        id=1,
        channel_id=100,
        telegram_msg_id=99,
        date=datetime(2026, 9, 17, tzinfo=UTC),
        text="x",
        media=[
            MediaDTO(
                type=MediaType.PHOTO,
                mime_type="image/jpeg",
                file_name="x.jpg",
                file_size=1024,
                width=10,
                height=10,
                object_key=None,  # 下载前未填
                object_backend="local",
                download_status=MediaDownloadStatus.DOWNLOADING,
            )
        ],
    )

    received: list[MediaDownloaded] = []
    bus.subscribe(MediaDownloaded, lambda e: received.append(e))

    # 启动 worker,塞一条,等它消费完
    worker_task = asyncio.create_task(mon._download_worker())
    await mon._download_queue.put((msg, 0))  # msg + media_idx

    # 等 worker 处理完(save_message 被调一次)— wait_for 比 for+sleep 更声明式
    assert await wait_for(lambda: storage.save_message.await_count >= 1, timeout=2.0), (
        "download_worker 没在 2s 内调 save_message"
    )
    # cancel worker 退出
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

    # 断言
    assert mon.downloader.download_one.await_count == 1
    storage.save_message.assert_awaited_once()
    assert len(received) == 1
    assert received[0].channel_id == 100
    assert received[0].telegram_msg_id == 99
    assert received[0].media is not None
    assert received[0].media.download_status == MediaDownloadStatus.DONE


async def test_download_worker_emits_progress_events() -> None:
    """worker 透传 downloader 的 progress callback → 发 `MediaDownloadProgress`。

    关键:download_one 拿到 progress_callback 后,call 它(模拟下载进度)。
    """
    mon, bus, _storage = _make_monitor_with_downloader()

    # 拦截 download_one,调 progress_callback 后返 DTO
    progress_calls: list = []

    async def fake_download_one(*, msg_pk, media, progress_callback):
        # 模拟 2 次进度回调
        await progress_callback(downloaded=512, total=1024)
        await progress_callback(downloaded=1024, total=1024)
        progress_calls.append((msg_pk, media))
        return MediaDTO(
            type=MediaType.PHOTO,
            mime_type="image/jpeg",
            file_name="x.jpg",
            file_size=1024,
            width=10,
            height=10,
            object_key="media/x.jpg",
            object_backend="local",
            download_status=MediaDownloadStatus.DONE,
        )

    mon.downloader.download_one = fake_download_one

    received: list[MediaDownloadProgress] = []
    bus.subscribe(MediaDownloadProgress, lambda e: received.append(e))

    msg = MessageDTO(
        id=1,
        channel_id=200,
        telegram_msg_id=42,
        date=datetime(2026, 9, 17, tzinfo=UTC),
        text="x",
        media=[
            MediaDTO(
                type=MediaType.PHOTO,
                mime_type="image/jpeg",
                file_name="y.jpg",
                file_size=1024,
                width=10,
                height=10,
                object_key=None,
                object_backend="local",
                download_status=MediaDownloadStatus.DOWNLOADING,
            )
        ],
    )

    worker_task = asyncio.create_task(mon._download_worker())
    await mon._download_queue.put((msg, 0))

    # 等 worker 跑完(emit 第一个 progress callback)
    assert await wait_for(lambda: len(progress_calls) >= 1, timeout=2.0), (
        "download_worker 没在 2s 内 emit progress callback"
    )
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

    # 2 次 progress callback → 2 个 MediaDownloadProgress 事件
    assert len(received) == 2
    assert received[0].channel_id == 200
    assert received[0].telegram_msg_id == 42
    assert received[0].media_idx == 0
    assert received[0].downloaded == 512
    assert received[0].total == 1024
    assert received[1].downloaded == 1024


async def test_download_worker_swallows_download_exception() -> None:
    """download_one 抛异常 → worker 不死,改写 FAILED 状态 + 发 MediaDownloaded。"""
    mon, bus, storage = _make_monitor_with_downloader()

    # 第一次 download_one 抛 RuntimeError,第二次返成功
    call_count = {"n": 0}

    async def fake_download_one(*, msg_pk, media, progress_callback):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("network blip")
        return MediaDTO(
            type=MediaType.PHOTO,
            mime_type="image/jpeg",
            file_name="x.jpg",
            file_size=1024,
            width=10,
            height=10,
            object_key="media/x.jpg",
            object_backend="local",
            download_status=MediaDownloadStatus.DONE,
        )

    mon.downloader.download_one = fake_download_one

    received: list[MediaDownloaded] = []
    bus.subscribe(MediaDownloaded, lambda e: received.append(e))

    msg = MessageDTO(
        id=1,
        channel_id=300,
        telegram_msg_id=1,
        date=datetime(2026, 9, 17, tzinfo=UTC),
        text="x",
        media=[
            MediaDTO(
                type=MediaType.PHOTO,
                mime_type="image/jpeg",
                file_name="z.jpg",
                file_size=1024,
                width=10,
                height=10,
                object_key=None,
                object_backend="local",
                download_status=MediaDownloadStatus.DOWNLOADING,
            )
        ],
    )

    worker_task = asyncio.create_task(mon._download_worker())
    # 塞 2 条:第一条失败,第二条成功
    await mon._download_queue.put((msg, 0))
    await mon._download_queue.put((msg, 0))

    # 等 worker 跑完 2 条(wait_for 比 for+sleep 更声明式)
    assert await wait_for(
        lambda: call_count["n"] >= 2 and storage.save_message.await_count >= 2,
        timeout=2.0,
    ), (
        f"download_worker 没在 2s 内跑完 2 条:count={call_count['n']}, saved={storage.save_message.await_count}"
    )
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

    # worker 没死:2 条都处理了
    assert call_count["n"] == 2
    # 2 个 MediaDownloaded:第一个 FAILED,第二个 DONE
    assert len(received) == 2
    assert received[0].media is not None
    assert received[0].media.download_status == MediaDownloadStatus.FAILED
    assert received[0].media.download_error is not None
    assert "network blip" in received[0].media.download_error
    assert received[1].media is not None
    assert received[1].media.download_status == MediaDownloadStatus.DONE
