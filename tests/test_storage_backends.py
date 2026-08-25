"""4 存储后端 parity(2026-08-25 PR #3)— list_media + count_media_by_object_key 行为对齐。

只覆盖能跑的两后端(InMemory + JsonlFileStore)— Postgres / Mongo
无本地集成测试桩(`asyncpg` 需要 PG server,`mongomock_motor` 未装);
contract 一致性由它们各自的 backend unit test 保证(后续按需补)。

断言集中在 `test_*_parity_*` 系列 — 同一组 input,两后端产出必须一致
(顺序由 backend 自己定,顺序差异在 spec 里说明)。
"""
from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio

from tests.conftest import InMemoryRepository, make_message
from tgmonitor.core.dto import (
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
)
from tgmonitor.core.storage.jsonl_store import JsonlFileStore

pytestmark = pytest.mark.asyncio


# ---- 共享 fixture:种子数据 ----

def _photo(idx: int = 0, status: MediaDownloadStatus = MediaDownloadStatus.DONE,
           object_key: str | None = "media/photo_a.jpg",
           file_name: str = "photo_a.jpg") -> MediaDTO:
    return MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name=file_name,
        file_size=1024,
        width=800,
        height=600,
        object_key=object_key,
        object_backend="local",
        download_status=status,
    )


def _video(idx: int = 0, status: MediaDownloadStatus = MediaDownloadStatus.DONE,
           object_key: str | None = "media/clip_a.mp4",
           file_name: str = "clip_a.mp4") -> MediaDTO:
    return MediaDTO(
        type=MediaType.VIDEO,
        mime_type="video/mp4",
        file_name=file_name,
        file_size=5_000_000,
        duration=30,
        object_key=object_key,
        object_backend="local",
        download_status=status,
    )


@pytest_asyncio.fixture
async def in_mem_repo() -> InMemoryRepository:
    repo = InMemoryRepository()
    await repo.save_message(make_message(
        channel_id=100, msg_id=1, date=datetime(2026, 1, 1, 10),
        media=[_photo(0, MediaDownloadStatus.DONE, "media/photo_a.jpg", "photo_a.jpg")],
    ))
    await repo.save_message(make_message(
        channel_id=100, msg_id=2, date=datetime(2026, 1, 2, 10),
        media=[
            _video(0, MediaDownloadStatus.DONE, "media/clip_a.mp4", "clip_a.mp4"),
            _photo(1, MediaDownloadStatus.FAILED, None, "failed.jpg"),
        ],
    ))
    await repo.save_message(make_message(
        channel_id=200, msg_id=10, date=datetime(2026, 1, 3, 10),
        media=[_photo(0, MediaDownloadStatus.DONE, "media/photo_a.jpg", "photo_a.jpg")],
    ))
    await repo.save_message(make_message(
        channel_id=300, msg_id=5, date=datetime(2026, 1, 4, 10),
        media=[_video(0, MediaDownloadStatus.PENDING, None, "pending.mp4")],
    ))
    return repo


@pytest_asyncio.fixture
async def jsonl_repo(tmp_path):
    repo = JsonlFileStore(root=tmp_path)
    await repo.connect()
    await repo.init_schema()
    # 频道需要「已订阅」才会被 jsonl 后端扫描到(list_media 走 list_subscribed_channels)
    from tgmonitor.core.dto import ChannelDTO
    for cid in (100, 200, 300):
        await repo.upsert_channel(ChannelDTO(id=cid, title=f"#{cid}"))
        await repo.set_channel_subscribed(cid, True)
    await repo.save_message(make_message(
        channel_id=100, msg_id=1, date=datetime(2026, 1, 1, 10),
        media=[_photo(0, MediaDownloadStatus.DONE, "media/photo_a.jpg", "photo_a.jpg")],
    ))
    await repo.save_message(make_message(
        channel_id=100, msg_id=2, date=datetime(2026, 1, 2, 10),
        media=[
            _video(0, MediaDownloadStatus.DONE, "media/clip_a.mp4", "clip_a.mp4"),
            _photo(1, MediaDownloadStatus.FAILED, None, "failed.jpg"),
        ],
    ))
    await repo.save_message(make_message(
        channel_id=200, msg_id=10, date=datetime(2026, 1, 3, 10),
        media=[_photo(0, MediaDownloadStatus.DONE, "media/photo_a.jpg", "photo_a.jpg")],
    ))
    await repo.save_message(make_message(
        channel_id=300, msg_id=5, date=datetime(2026, 1, 4, 10),
        media=[_video(0, MediaDownloadStatus.PENDING, None, "pending.mp4")],
    ))
    return repo


# ---- list_media parity ----

async def test_in_mem_list_media_no_filter_returns_all(in_mem_repo):
    rows = await in_mem_repo.list_media()
    # 4 条:msg1-photo / msg2-video / msg2-photo-failed / msg10-photo / msg5-video-pending
    assert len(rows) == 5


async def test_jsonl_list_media_no_filter_returns_all(jsonl_repo):
    rows = await jsonl_repo.list_media()
    assert len(rows) == 5


async def test_in_mem_list_media_filter_by_status_done(in_mem_repo):
    rows = await in_mem_repo.list_media(status=MediaDownloadStatus.DONE)
    # msg1 photo DONE / msg2-video DONE / msg10 photo DONE = 3
    assert len(rows) == 3
    assert all(r[2].download_status == MediaDownloadStatus.DONE for r in rows)


async def test_jsonl_list_media_filter_by_status_done(jsonl_repo):
    rows = await jsonl_repo.list_media(status=MediaDownloadStatus.DONE)
    assert len(rows) == 3
    assert all(r[2].download_status == MediaDownloadStatus.DONE for r in rows)


async def test_in_mem_list_media_filter_by_channel(in_mem_repo):
    rows = await in_mem_repo.list_media(channel_ids=[100])
    # ch=100 的 3 条 media(msg1 + msg2×2)
    assert len(rows) == 3
    assert all(r[0].channel_id == 100 for r in rows)


async def test_jsonl_list_media_filter_by_channel(jsonl_repo):
    rows = await jsonl_repo.list_media(channel_ids=[100])
    assert len(rows) == 3
    assert all(r[0].channel_id == 100 for r in rows)


async def test_in_mem_list_media_filter_by_type(in_mem_repo):
    rows = await in_mem_repo.list_media(media_type=MediaType.PHOTO)
    # 3 photo:msg1 / msg2-failed / msg10
    assert len(rows) == 3
    assert all(r[2].type == MediaType.PHOTO for r in rows)


async def test_jsonl_list_media_filter_by_type(jsonl_repo):
    rows = await jsonl_repo.list_media(media_type=MediaType.PHOTO)
    assert len(rows) == 3


async def test_in_mem_list_media_search_case_insensitive(in_mem_repo):
    # "PHOTO" 大写应命中 photo_a.jpg(大小写无关)
    rows = await in_mem_repo.list_media(search="PHOTO")
    assert len(rows) == 2  # photo_a.jpg 出现两次(msg1 / msg10 共用 file_name)


async def test_jsonl_list_media_search_case_insensitive(jsonl_repo):
    rows = await jsonl_repo.list_media(search="PHOTO")
    assert len(rows) == 2


async def test_in_mem_list_media_limit_offset(in_mem_repo):
    rows = await in_mem_repo.list_media(limit=2)
    assert len(rows) == 2
    rows2 = await in_mem_repo.list_media(limit=2, offset=2)
    assert len(rows2) == 2
    # offset 跳过前 2,后续不重叠
    keys_a = {(r[0].channel_id, r[0].telegram_msg_id, r[1]) for r in rows}
    keys_b = {(r[0].channel_id, r[0].telegram_msg_id, r[1]) for r in rows2}
    assert not (keys_a & keys_b)


async def test_jsonl_list_media_limit_offset(jsonl_repo):
    rows = await jsonl_repo.list_media(limit=2)
    assert len(rows) == 2
    rows2 = await jsonl_repo.list_media(limit=2, offset=2)
    assert len(rows2) == 2
    keys_a = {(r[0].channel_id, r[0].telegram_msg_id, r[1]) for r in rows}
    keys_b = {(r[0].channel_id, r[0].telegram_msg_id, r[1]) for r in rows2}
    assert not (keys_a & keys_b)


# ---- count_media_by_object_key parity ----

async def test_in_mem_count_refcount_shared_key(in_mem_repo):
    # photo_a.jpg 在 msg1 + msg10 两次出现 → refcount=2
    assert await in_mem_repo.count_media_by_object_key("media/photo_a.jpg") == 2


async def test_jsonl_count_refcount_shared_key(jsonl_repo):
    assert await jsonl_repo.count_media_by_object_key("media/photo_a.jpg") == 2


async def test_in_mem_count_refcount_unique_key(in_mem_repo):
    # clip_a.mp4 只在 msg2 出现 → refcount=1
    assert await in_mem_repo.count_media_by_object_key("media/clip_a.mp4") == 1


async def test_jsonl_count_refcount_unique_key(jsonl_repo):
    assert await jsonl_repo.count_media_by_object_key("media/clip_a.mp4") == 1


async def test_in_mem_count_refcount_missing_key(in_mem_repo):
    assert await in_mem_repo.count_media_by_object_key("media/never.jpg") == 0


async def test_jsonl_count_refcount_missing_key(jsonl_repo):
    assert await jsonl_repo.count_media_by_object_key("media/never.jpg") == 0


async def test_in_mem_count_refcount_empty_key(in_mem_repo):
    # 空字符串:没 media 用 object_key="" — 返 0
    assert await in_mem_repo.count_media_by_object_key("") == 0


async def test_jsonl_count_refcount_empty_key(jsonl_repo):
    assert await jsonl_repo.count_media_by_object_key("") == 0