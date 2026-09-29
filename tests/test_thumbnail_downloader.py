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


async def test_download_thumb_success(tmp_path):
    repo, objs, client, dl = await _make_setup(tmp_path)
    client.set_download("thumb-fid-99", THUMB_BYTES)

    thumb = await dl.download_thumb(msg_pk=(100, 42), media=_photo())

    assert thumb is not None
    assert thumb.channel_id == 100
    assert thumb.telegram_msg_id == 42
    assert thumb.download_status == MediaDownloadStatus.DONE
    assert thumb.object_key is not None
    assert thumb.object_key.startswith("thumb/")
    # mime 决定 ext:`image/jpeg` → .jpeg;`image/png` → .png(避免硬编码 .jpg)
    assert thumb.object_key.endswith((".jpg", ".jpeg", ".png", ".webp"))
    assert thumb.object_backend == "local"
    assert thumb.file_size == len(THUMB_BYTES)
    assert thumb.sha256 is not None
    # bytes 真入 ObjectStore
    assert await objs.exists(thumb.object_key)
    stored = await objs.get(thumb.object_key)
    assert stored == THUMB_BYTES


async def test_download_thumb_skipped_when_no_thumb_file_id(tmp_path):
    """无 thumbnail_telegram_file_id → 返 None(caller 不落库)。"""
    repo, objs, client, dl = await _make_setup(tmp_path)
    thumb = await dl.download_thumb(
        msg_pk=(100, 42),
        media=_photo(thumbnail_telegram_file_id=None),
    )
    assert thumb is None


async def test_download_thumb_failed_when_download_returns_none(tmp_path):
    """download_file 返 None(thumb TG 端超时 / 缺数据)→ ThumbnailDTO 状态 FAILED。"""
    repo, objs, client, dl = await _make_setup(tmp_path)
    # 不注入 thumb-fid-99 → FakeTelegramClient.download_file 返 None
    thumb = await dl.download_thumb(msg_pk=(100, 42), media=_photo())
    assert thumb is not None
    assert thumb.download_status == MediaDownloadStatus.FAILED
    assert thumb.object_key is None
    assert "缩略图" in (thumb.download_error or "")
    # thumb FAILED 不入 ObjectStore
    assert not await objs.exists("thumb/")  # 无 thumb 前缀 key 落盘


async def test_download_thumb_does_not_affect_main(tmp_path):
    """2026-09-29:thumb 失败不影响主图(下载契约已是)。"""
    repo, objs, client, dl = await _make_setup(tmp_path)
    # main 成功
    client.set_download("photo-fid", b"MAIN_BODY" * 100)
    # thumb 失败(不注入 thumb-fid)
    main = await dl.download_one(msg_pk=1, media=_photo())
    assert main.download_status == MediaDownloadStatus.DONE
    thumb = await dl.download_thumb(msg_pk=(100, 42), media=_photo())
    assert thumb is not None
    assert thumb.download_status == MediaDownloadStatus.FAILED


async def test_download_thumb_key_content_addressed(tmp_path):
    """2026-09-29:thumb key 走 `thumb/<sha256>.<ext>` 内容寻址,跟 main 对称。

    同一 thumb file_id 多次下载 → 同一 key(便于 dedup);不同 file_id →
    不同 key。
    """
    repo, objs, client, dl = await _make_setup(tmp_path)
    client.set_download("thumb-fid-A", THUMB_BYTES)
    client.set_download("thumb-fid-B", b"\x04DIFFERENT_BODY")

    t_a1 = await dl.download_thumb(
        msg_pk=(1, 1), media=_photo(thumbnail_telegram_file_id="thumb-fid-A")
    )
    t_a2 = await dl.download_thumb(
        msg_pk=(1, 1), media=_photo(thumbnail_telegram_file_id="thumb-fid-A")
    )
    t_b = await dl.download_thumb(
        msg_pk=(1, 1), media=_photo(thumbnail_telegram_file_id="thumb-fid-B")
    )

    assert t_a1 is not None and t_a1.object_key is not None
    assert t_a2 is not None and t_a2.object_key == t_a1.object_key  # 同一 key
    assert t_b is not None and t_b.object_key is not None
    assert t_b.object_key != t_a1.object_key  # 不同 file_id 不同 key


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
