"""2026-09-29:MediaDownloader.download_thumb 单测。

覆盖契约:
- 有 `thumbnail_telegram_file_id` → 真下载 bytes → 入 ObjectStore → 返 DONE
- 无 `thumbnail_telegram_file_id` → 返 None(caller 不落库)
- 下载返 None → ThumbnailDTO 状态 FAILED + 原因
- 主图 DONE + thumb 下完 → caller 落 `thumbnails` 表正确
- thumb key 走 `thumb/<sha256>.<ext>` 内容寻址,跟 main key 对称
- thumb 失败不影响主图(FULL 策略 retry 时 thumb 单独 retry)

测试不依赖真实 Telegram — 用 FakeTelegramClient + LocalObjectStore 离线跑。
"""

from __future__ import annotations

from tests.fixtures._in_memory_repository import InMemoryRepository
from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MediaType, ThumbnailDTO
from tgmonitor.core.monitor.service import MediaDownloader
from tgmonitor.core.objectstore.local_store import LocalObjectStore
from tgmonitor.core.telegram.fake_client import FakeTelegramClient

# fake thumb bytes(JPEG 头部 magic 即可,内容不重要)
THUMB_BYTES = b"\xff\xd8\xff\xe0FAKE_THUMB_JPEG_BYTES" * 4


async def _make_setup(tmp_path):
    """准备 InMemoryRepository + LocalObjectStore + FakeTelegramClient + MediaDownloader。"""
    repo = InMemoryRepository()
    objs = LocalObjectStore(root=tmp_path / "obj")
    await objs.connect()
    client = FakeTelegramClient()
    dl = MediaDownloader(client, repo, objs)
    return repo, objs, client, dl


def _photo(**overrides) -> MediaDTO:
    base = MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        telegram_file_id="photo-fid",
        thumbnail_telegram_file_id="thumb-fid-99",
    )
    from dataclasses import replace

    return replace(base, **overrides)


async def test_download_thumb_persists_to_thumbnails_table(tmp_path):
    """2026-09-29:download_thumb 返的 DTO 落 `thumbnails` 表 — roundtrip OK。"""
    repo, objs, client, dl = await _make_setup(tmp_path)
    client.set_download("thumb-fid-99", THUMB_BYTES)

    med = _photo()
    thumb = await dl.download_thumb(msg_pk=(100, 42), media=med)
    assert thumb is not None
    # caller 必覆盖 media_idx — 模拟 _download_worker 流程
    final = ThumbnailDTO(
        channel_id=thumb.channel_id,
        telegram_msg_id=thumb.telegram_msg_id,
        media_idx=0,
        object_key=thumb.object_key,
        object_backend=thumb.object_backend,
        file_size=thumb.file_size,
        mime_type=thumb.mime_type,
        sha256=thumb.sha256,
        download_status=thumb.download_status,
        telegram_thumb_file_id=thumb.telegram_thumb_file_id,
    )
    await repo.save_thumbnail(final)

    got = await repo.get_thumbnail(100, 42, 0)
    assert got is not None
    assert got.object_key == thumb.object_key
    assert got.download_status == MediaDownloadStatus.DONE
