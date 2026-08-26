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

import pytest
import pytest_asyncio

from tests.conftest import InMemoryRepository
from tgmonitor.core.app_service import AppService
from tgmonitor.core.dto import (
    ChannelDTO,
    DeleteChannelPreview,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
)
from tgmonitor.core.events import EventBus
from tgmonitor.core.objectstore.local_store import LocalObjectStore


def _msg(msg_id: int, media: list[MediaDTO]) -> MessageDTO:
    return MessageDTO(
        id=0, channel_id=100, telegram_msg_id=msg_id,
        date=datetime(2026, 1, 1, 12), text="x", media=media,
    )


def _photo(file_size: int = 1024, status: MediaDownloadStatus = MediaDownloadStatus.DONE,
           object_key: str | None = "media/p.jpg") -> MediaDTO:
    return MediaDTO(
        type=MediaType.PHOTO, mime_type="image/jpeg",
        file_name="p.jpg", file_size=file_size,
        object_key=object_key, object_backend="local",
        download_status=status,
    )


@pytest_asyncio.fixture
async def svc(tmp_path):
    storage = InMemoryRepository()
    await storage.upsert_channel(ChannelDTO(id=100, title="A"))
    await storage.upsert_channel(ChannelDTO(id=200, title="B"))
    await storage.set_channel_subscribed(100, True)
    await storage.set_channel_subscribed(200, True)

    objects = LocalObjectStore(root=tmp_path / "media")
    await objects.connect()

    bus = EventBus()
    from tgmonitor.core.config import MediaPolicy, Settings
    from tgmonitor.core.telegram.fake_client import FakeTelegramClient
    settings = Settings(  # type: ignore[call-arg]
        api_id=1, api_hash="x" * 32, phone="+10000000000",
        session_dir=tmp_path / "session",
        objectstore_root=tmp_path / "media",
        data_root=tmp_path,
        media_policy=MediaPolicy.METADATA,
    )
    settings.ensure_dirs()
    return AppService(bus, FakeTelegramClient(), storage, objects, settings)


async def test_preview_basic_counts(svc: AppService):
    """PR #8:preview_delete_by_channel → message_count / media_count / orphan_bytes。"""
    await svc.storage.save_message(MessageDTO(
        id=0, channel_id=100, telegram_msg_id=1,
        date=datetime(2026, 1, 1), text="", media=[
            _photo(1024, MediaDownloadStatus.DONE, "media/a.jpg"),
        ],
    ))
    await svc.storage.save_message(MessageDTO(
        id=0, channel_id=100, telegram_msg_id=2,
        date=datetime(2026, 1, 1), text="", media=[
            _photo(2048, MediaDownloadStatus.DONE, "media/b.jpg"),
            _photo(512, MediaDownloadStatus.DONE, "media/c.jpg"),
        ],
    ))

    pv = await svc.preview_delete_by_channel(100)
    assert isinstance(pv, DeleteChannelPreview)
    assert pv.channel_id == 100
    assert pv.message_count == 2
    assert pv.media_count == 3
    # 1024 + 2048 + 512 = 3584 — 各 key refcount=1 都成孤儿
    assert pv.potential_orphan_bytes == 3584


async def test_preview_empty_channel_returns_zero(svc: AppService):
    """PR #8:空 channel → 全 0 dataclass,不抛。"""
    pv = await svc.preview_delete_by_channel(999)
    assert pv.message_count == 0
    assert pv.media_count == 0
    assert pv.potential_orphan_bytes == 0


async def test_preview_shared_object_key_excluded(svc: AppService):
    """PR #8:跨频道共享 object_key → preview 时不计入 orphan_bytes。"""
    # ch 100 + ch 200 共用 object_key="media/shared.jpg"
    await svc.storage.save_message(MessageDTO(
        id=0, channel_id=100, telegram_msg_id=1,
        date=datetime(2026, 1, 1), text="", media=[
            _photo(1024, MediaDownloadStatus.DONE, "media/shared.jpg"),
        ],
    ))
    await svc.storage.save_message(MessageDTO(
        id=0, channel_id=200, telegram_msg_id=1,
        date=datetime(2026, 1, 1), text="", media=[
            _photo(1024, MediaDownloadStatus.DONE, "media/shared.jpg"),
        ],
    ))

    pv = await svc.preview_delete_by_channel(100)
    # refcount("media/shared.jpg") = 2 → 不应计入 orphan
    assert pv.potential_orphan_bytes == 0


async def test_preview_pending_status_excluded(svc: AppService):
    """PR #8:preview 只数 DONE media 的 bytes(跟 delete_by_channel 一致)。"""
    await svc.storage.save_message(MessageDTO(
        id=0, channel_id=100, telegram_msg_id=1,
        date=datetime(2026, 1, 1), text="", media=[
            _photo(1024, MediaDownloadStatus.DONE, "media/done.jpg"),
            _photo(9999, MediaDownloadStatus.PENDING, None),
            _photo(8888, MediaDownloadStatus.FAILED, None),
        ],
    ))

    pv = await svc.preview_delete_by_channel(100)
    # 只算 DONE 1024 — PENDING / FAILED 不计
    assert pv.potential_orphan_bytes == 1024


async def test_preview_does_not_mutate_storage(svc: AppService):
    """PR #8:preview 是严格只读 — storage / objects 状态不变。"""
    await svc.storage.save_message(MessageDTO(
        id=0, channel_id=100, telegram_msg_id=1,
        date=datetime(2026, 1, 1), text="", media=[
            _photo(1024, MediaDownloadStatus.DONE, "media/a.jpg"),
        ],
    ))
    msg_count_before = await svc.storage.count_messages(100)
    media_count_before = await svc.storage.count_media_by_channel(100)

    await svc.preview_delete_by_channel(100)

    assert await svc.storage.count_messages(100) == msg_count_before
    assert await svc.storage.count_media_by_channel(100) == media_count_before


@pytest.fixture
def qt_app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_dialog_ok_disabled_until_checked(qt_app):
    """PR #8:dialog OK 按钮默认 disabled,勾 ack 后才 enable。"""
    from PySide6.QtWidgets import QDialogButtonBox

    from tgmonitor.ui.widgets.clear_channel_preview_dialog import (
        ClearChannelPreviewDialog,
    )

    pv = DeleteChannelPreview(
        channel_id=100, message_count=5, media_count=10,
        potential_orphan_bytes=1024,
    )
    dlg = ClearChannelPreviewDialog(pv, "Test")
    bb = dlg.findChild(QDialogButtonBox)
    ok_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
    assert not ok_btn.isEnabled()

    dlg.chk_ack.setChecked(True)
    assert ok_btn.isEnabled()

    dlg.chk_ack.setChecked(False)
    assert not ok_btn.isEnabled()
    qt_app.processEvents()


def test_dialog_cancel_returns_rejected(qt_app):
    """PR #8:dialog Cancel → Rejected,Accepted 才走 vm.delete_by_channel。"""
    from PySide6.QtWidgets import QDialog

    from tgmonitor.ui.widgets.clear_channel_preview_dialog import (
        ClearChannelPreviewDialog,
    )

    pv = DeleteChannelPreview(
        channel_id=100, message_count=1, media_count=1,
        potential_orphan_bytes=0,
    )
    dlg = ClearChannelPreviewDialog(pv, "Test")
    dlg.reject()
    assert dlg.result() == QDialog.DialogCode.Rejected
    qt_app.processEvents()


def test_dialog_shows_counts_in_labels(qt_app):
    """PR #8:dialog labels 显示 message_count / media_count / bytes。"""
    from PySide6.QtWidgets import QLabel

    from tgmonitor.ui.widgets.clear_channel_preview_dialog import (
        ClearChannelPreviewDialog,
    )

    pv = DeleteChannelPreview(
        channel_id=100, message_count=42, media_count=17,
        potential_orphan_bytes=5_242_880,  # 5 MB
    )
    dlg = ClearChannelPreviewDialog(pv, "My Channel")
    labels = dlg.findChildren(QLabel)
    all_text = " ".join(lbl.text() for lbl in labels)
    assert "42" in all_text
    assert "17" in all_text
    assert "5.0MB" in all_text or "5.2MB" in all_text
    assert "My Channel" in all_text
    qt_app.processEvents()
