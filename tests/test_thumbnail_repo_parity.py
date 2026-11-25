"""2026-09-29:缩略图独立表多后端 parity。

策略(v2.0.0):
- `InMemoryRepository`(in-process 替身,基准)
- `SqliteRepository`(v2.0.0 默认后端,真实文件 IO)

PG 真集在 `tests/integration/test_pg_repo_parity.py` 单独覆盖
(需要 testcontainers,Docker 不可用时自动 skip)。

覆盖契约:
- `save_thumbnail` upsert:同三元组二次 save → 取出来是后值
- `get_thumbnail` 不存在返 None
- `delete_thumbnail` 不存在不抛(idempotent)
- `list_thumbnails_for_message` 按 media_idx ASC 排序
- `count_thumbnails_by_object_key` 跨消息聚合
- `delete_message` 级联清 thumb(SQL 后端走 FK CASCADE)
- 各后端行为一致(无 race / 顺序差异)

注:SQL 后端 thumbnails 表有 FK → 父消息必须先存在,`_seed_parent`
显式建档(InMemory 无 FK 概念,调了同样无害)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import pytest
import pytest_asyncio

from tests.fixtures._in_memory_repository import InMemoryRepository
from tgmonitor.core.dto import MediaDownloadStatus, MessageDTO, ThumbnailDTO
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.storage.sqlite_repo import SqliteRepository

# ---- fixtures ----


@pytest_asyncio.fixture
async def inmem_repo() -> AsyncIterator[InMemoryRepository]:
    yield InMemoryRepository()


@pytest_asyncio.fixture
async def sqlite_repo(tmp_path) -> AsyncIterator[SqliteRepository]:
    repo = SqliteRepository(path=tmp_path / "thumb_parity.db")
    await repo.connect()
    await repo.init_schema()
    try:
        yield repo
    finally:
        await repo.close()


@pytest.fixture(params=["inmem", "sqlite"])
def repo(request, inmem_repo, sqlite_repo) -> StorageRepository:
    """双后端 parametrize(InMemory + SQLite)。"""
    if request.param == "inmem":
        return inmem_repo
    return sqlite_repo


async def _seed_parent(repo: StorageRepository, channel_id: int, msg_id: int) -> None:
    """SQL 后端 FK 要求父消息先存在;InMemory 无 FK,调了无害。"""
    await repo.save_message(
        MessageDTO(
            id=0,
            channel_id=channel_id,
            telegram_msg_id=msg_id,
            date=datetime(2026, 1, 1),
            text="parent",
            media=[],
        )
    )


def _thumb(**overrides) -> ThumbnailDTO:
    """造一个默认 ThumbnailDTO;`replace` 风格覆盖字段。"""
    base = ThumbnailDTO(
        channel_id=100,
        telegram_msg_id=1,
        media_idx=0,
        object_key="thumb/abc.jpg",
        object_backend="local",
        file_size=4096,
        mime_type="image/jpeg",
        width=90,
        height=90,
        sha256="abc123",
        download_status=MediaDownloadStatus.DONE,
        telegram_thumb_file_id="tg-33",
    )
    from dataclasses import replace

    return replace(base, **overrides)


# ---- tests ----


async def test_save_then_get_roundtrip(repo: StorageRepository) -> None:
    """save → get 完整 roundtrip(包含 None 与非 None 字段)。"""
    await _seed_parent(repo, 100, 1)
    t = _thumb(file_size=2048, sha256="deadbeef")
    await repo.save_thumbnail(t)
    got = await repo.get_thumbnail(100, 1, 0)
    assert got is not None
    assert got.channel_id == 100
    assert got.telegram_msg_id == 1
    assert got.media_idx == 0
    assert got.object_key == "thumb/abc.jpg"
    assert got.object_backend == "local"
    assert got.file_size == 2048
    assert got.sha256 == "deadbeef"
    assert got.download_status == MediaDownloadStatus.DONE
    assert got.telegram_thumb_file_id == "tg-33"
