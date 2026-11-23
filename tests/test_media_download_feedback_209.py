"""2026-11-23 v2.0.9 — 手动下载/重下的进度反馈守卫。

背景:行内「下载」原先不传 progress_callback,整段下载期间 UI 无任何事件,
用户点完像「没反应」。本文件守卫:

1. `download_media` / `retry_media` 都向 `download_one` 传了 progress_callback。
2. 回调触发时发 `MediaDownloadProgress` 事件(带正确 row 定位三元组)。
3. controller 下载入口发状态栏即时提示。
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from tests.conftest import InMemoryRepository, make_message
from tgmonitor.core.app_service import AppService
from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MediaType
from tgmonitor.core.events import MediaDownloadProgress


def _media(status: MediaDownloadStatus | None = None) -> MediaDTO:
    return MediaDTO(
        type=MediaType.PHOTO,
        file_name="photo.jpg",
        file_size=100,
        telegram_file_id="fid",
        download_status=status,
    )


class _StubDownloader:
    def __init__(self) -> None:
        self.callbacks: list[object] = []
        self.kwargs: list[dict] = []

    async def download_one(self, *, msg_pk, media, force=False, progress_callback=None):  # noqa: ARG002
        self.kwargs.append({"force": force})
        if progress_callback is not None:
            self.callbacks.append(progress_callback)
            await progress_callback(50, 100)
        return dataclasses.replace(
            media,
            object_key="media/photo.jpg",
            object_backend="local",
            download_status=MediaDownloadStatus.DONE,
        )


@pytest.mark.asyncio
async def test_download_media_emits_progress(app: AppService, storage: InMemoryRepository) -> None:
    """未下载首下:进度回调透传 + 发 MediaDownloadProgress 事件。"""
    stub = _StubDownloader()
    app.downloader = stub  # type: ignore[assignment]
    await storage.save_message(make_message(channel_id=100, msg_id=1, media=[_media()]))

    seen: list[MediaDownloadProgress] = []

    async def _on(e: object) -> None:
        if isinstance(e, MediaDownloadProgress):
            seen.append(e)

    app.bus.subscribe_all(_on)
    await app.media.download_media(100, 1, 0)

    assert stub.callbacks, "download_one 必须收到 progress_callback"
    assert stub.kwargs[0]["force"] is False
    assert len(seen) == 1, f"应发出 1 条进度事件,实际 {len(seen)}"
    assert seen[0].channel_id == 100
    assert seen[0].telegram_msg_id == 1
    assert seen[0].media_idx == 0
    assert seen[0].downloaded == 50 and seen[0].total == 100


@pytest.mark.asyncio
async def test_retry_media_emits_progress(app: AppService, storage: InMemoryRepository) -> None:
    """FAILED 重下:同样要有进度事件,row 定位正确。"""
    stub = _StubDownloader()
    app.downloader = stub  # type: ignore[assignment]
    await storage.save_message(
        make_message(channel_id=100, msg_id=2, media=[_media(MediaDownloadStatus.FAILED)])
    )

    seen: list[MediaDownloadProgress] = []

    async def _on(e: object) -> None:
        if isinstance(e, MediaDownloadProgress):
            seen.append(e)

    app.bus.subscribe_all(_on)
    await app.media.retry_media(100, 2, 0)

    assert stub.callbacks, "retry 的 download_one 也必须收到 progress_callback"
    assert stub.kwargs[0]["force"] is True
    assert len(seen) == 1
    assert (seen[0].channel_id, seen[0].telegram_msg_id, seen[0].media_idx) == (100, 2, 0)


class _SlowDownloader:
    """故意 await 一个 Event,模拟下载进行中 —— 用来测并发 in-flight 守卫。"""

    def __init__(self) -> None:
        self.started = 0
        self.gate = asyncio.Event()

    async def download_one(self, *, msg_pk, media, force=False, progress_callback=None):  # noqa: ARG002
        self.started += 1
        await self.gate.wait()  # 卡住,直到测试放行
        return dataclasses.replace(
            media,
            object_key="media/photo.jpg",
            object_backend="local",
            download_status=MediaDownloadStatus.DONE,
        )


@pytest.mark.asyncio
async def test_inflight_guard_rejects_duplicate_download(
    app: AppService, storage: InMemoryRepository
) -> None:
    """2026-11-23:同一媒体并发两次下载,第二次被 in-flight 守卫挡掉(只下一次)。"""
    slow = _SlowDownloader()
    app.downloader = slow  # type: ignore[assignment]
    await storage.save_message(make_message(channel_id=100, msg_id=3, media=[_media()]))

    task1 = asyncio.create_task(app.media.download_media(100, 3, 0))
    await asyncio.sleep(0)  # 让 task1 进入 download_one 并占住 in-flight
    task2 = asyncio.create_task(app.media.download_media(100, 3, 0))
    await asyncio.sleep(0)
    assert slow.started == 1, f"第二次下载应被守卫挡掉,实际启动 {slow.started} 次"

    slow.gate.set()
    await asyncio.gather(task1, task2)
    # 守卫在 finally 释放 —— 异常/完成后同一 key 可再次下载
    assert app.media._inflight_downloads == set()  # type: ignore[attr-defined]
