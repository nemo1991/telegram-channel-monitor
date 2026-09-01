"""下载进度事件 → Qt signal 转发测试 — 2026-09-01 v1.5.1 PR #B3。

`MonitorViewModel` 订阅 `MediaDownloadProgress` 事件 → emit Qt
`media_download_progress` signal。UI 端 media_manager 接到后刷新
status label。这里只测 VM 层透传,UI 渲染走 visual_regression。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from tgmonitor.core.events import EventBus, MediaDownloadProgress
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel


class _FakeApp:
    """VM 只需要 `bus` 属性;其它 AppService 接口 stub 掉。"""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus


def _make_message(channel_id: int = 100, msg_id: int = 42, media_count: int = 1):
    """构造测试用 MessageDTO;避开真实 TG 链路。"""
    from tgmonitor.core.dto import MediaDTO, MediaType, MessageDTO

    media = [
        MediaDTO(type=MediaType.PHOTO, file_name=f"m{i}.jpg") for i in range(media_count)
    ]
    return MessageDTO(
        id=1,
        channel_id=channel_id,
        telegram_msg_id=msg_id,
        date=datetime(2026, 9, 1, 12, 0, 0),
        text="photo",
        media=media,
    )


def _make_vm(bus: EventBus) -> MonitorViewModel:
    """建 VM 但**不**调 start (无 client);只测 _on_media_download_progress。"""
    loop = asyncio.new_event_loop()
    return MonitorViewModel(_FakeApp(bus), monitor=None, loop=loop)  # type: ignore[arg-type]


async def test_vm_subscribes_media_download_progress():
    """PR #B3:VM 在 _wire_bus 时订阅了 `MediaDownloadProgress` 事件。"""
    bus = EventBus()
    vm = _make_vm(bus)
    try:
        # 收到事件 → emit signal(用 subscribe_all 计数)
        captured: list[MediaDownloadProgress] = []
        vm.media_download_progress.connect(lambda e: captured.append(e))

        evt = MediaDownloadProgress(
            channel_id=100,
            telegram_msg_id=42,
            media_idx=0,
            downloaded=1024,
            total=4096,
        )
        await bus.publish(evt)
        # `_on_media_download_progress` 是 async,EventBus.publish 内部 await 它
        # → signal 同步 emit,Python 让出后回调完成
        assert len(captured) == 1
        assert captured[0] is evt
        # 内容也透传
        assert captured[0].downloaded == 1024
        assert captured[0].total == 4096
    finally:
        vm.loop.close()


async def test_vm_progress_signal_forwards_none_total():
    """PR #B3:`total=None`(file_size 未知)→ VM 也透传,UI fallback「已下载 X / ?」。"""
    bus = EventBus()
    vm = _make_vm(bus)
    try:
        captured: list[MediaDownloadProgress] = []
        vm.media_download_progress.connect(lambda e: captured.append(e))

        evt = MediaDownloadProgress(
            channel_id=200,
            telegram_msg_id=10,
            media_idx=2,
            downloaded=512,
            total=None,
        )
        await bus.publish(evt)
        assert len(captured) == 1
        assert captured[0].total is None
    finally:
        vm.loop.close()


async def test_vm_progress_signal_ignores_other_events():
    """PR #B3:VM 的 `_on_media_download_progress` isinstance 守卫 → 收到
    非 `MediaDownloadProgress` 事件不 emit。"""
    bus = EventBus()
    vm = _make_vm(bus)
    try:
        captured: list[MediaDownloadProgress] = []
        vm.media_download_progress.connect(lambda e: captured.append(e))

        # 发个无关事件 — VM 不该 emit 进度 signal
        from tgmonitor.core.events import MessageReceived

        await bus.publish(MessageReceived(message=None))
        assert captured == []
    finally:
        vm.loop.close()
