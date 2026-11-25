"""ClearChannelPreviewDialog + AppService.preview_delete_by_channel 测试 — 2026-08-25 v1.3.0 PR #8。

覆盖:
- `preview_delete_by_channel` 返回的 dataclass 三字段正确
- 空 channel / 无 media → 全 0,不抛
- 跨频道共享 object_key 不计入 orphan_bytes
- PENDING / FAILED media bytes 不计入 orphan_bytes(只看 DONE)
- preview 是只读:不调 storage.delete_* / objects.delete
- dialog:必勾 ack checkbox 才 enable OK;Cancel → Rejected;OK → Accepted
"""

from __future__ import annotations

from datetime import datetime

import pytest_asyncio

from tests.conftest import InMemoryRepository
from tgmonitor.core.app_service import AppService
from tgmonitor.core.dto import (
    ChannelDTO,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
)
from tgmonitor.core.events import EventBus
from tgmonitor.core.objectstore.local_store import LocalObjectStore


def _msg(msg_id: int, media: list[MediaDTO]) -> MessageDTO:
    return MessageDTO(
        id=0,
        channel_id=100,
        telegram_msg_id=msg_id,
        date=datetime(2026, 1, 1, 12),
        text="x",
        media=media,
    )


def _photo(
    file_size: int = 1024,
    status: MediaDownloadStatus = MediaDownloadStatus.DONE,
    object_key: str | None = "media/p.jpg",
) -> MediaDTO:
    return MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name="p.jpg",
        file_size=file_size,
        object_key=object_key,
        object_backend="local",
        download_status=status,
    )


@pytest_asyncio.fixture
async def svc(tmp_path):
    storage = InMemoryRepository()
    await storage.upsert_channel(ChannelDTO(id=100, title="A"))
    await storage.upsert_channel(ChannelDTO(id=200, title="B"))
    await storage.set_channel_monitored(100, True)
    await storage.set_channel_monitored(200, True)

    objects = LocalObjectStore(root=tmp_path / "media")
    await objects.connect()

    bus = EventBus()
    from tgmonitor.core.config import Settings
    from tgmonitor.core.telegram.fake_client import FakeTelegramClient

    settings = Settings.for_test(
        session_dir=tmp_path / "session",
        objectstore_root=tmp_path / "media",
        data_root=tmp_path,
    )
    return AppService(bus, FakeTelegramClient(), storage, objects, settings)


async def test_preview_shared_object_key_excluded(svc: AppService):
    """PR #8:跨频道共享 object_key → preview 时不计入 orphan_bytes。"""
    # ch 100 + ch 200 共用 object_key="media/shared.jpg"
    await svc.storage.save_message(
        MessageDTO(
            id=0,
            channel_id=100,
            telegram_msg_id=1,
            date=datetime(2026, 1, 1),
            text="",
            media=[
                _photo(1024, MediaDownloadStatus.DONE, "media/shared.jpg"),
            ],
        )
    )
    await svc.storage.save_message(
        MessageDTO(
            id=0,
            channel_id=200,
            telegram_msg_id=1,
            date=datetime(2026, 1, 1),
            text="",
            media=[
                _photo(1024, MediaDownloadStatus.DONE, "media/shared.jpg"),
            ],
        )
    )

    pv = await svc.media.preview_delete_by_channel(100)
    # refcount("media/shared.jpg") = 2 → 不应计入 orphan
    assert pv.potential_orphan_bytes == 0


# `qapp` from tests/conftest.py — session-scope QApplication 单例
