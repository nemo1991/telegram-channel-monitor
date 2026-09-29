"""2026-09-29:缩略图独立表三后端 parity。

策略(跟 `test_storage_backends_parity_introspect.py` 同模板):
- `InMemoryRepository`(in-process 替身,基准)
- `JsonlFileStore`(同步文件持久化)
- `MongoRepository` + `mongomock_motor`(in-process 模拟真 Mongo)

PG 真集在 `tests/integration/test_pg_repo_parity.py` 单独覆盖
(需要 testcontainers,Docker 不可用时自动 skip)。

覆盖契约:
- `save_thumbnail` upsert:同三元组二次 save → 取出来是后值
- `get_thumbnail` 不存在返 None
- `delete_thumbnail` 不存在不抛(idempotent)
- `list_thumbnails_for_message` 按 media_idx ASC 排序
- `count_thumbnails_by_object_key` 跨消息聚合
- 三后端行为一致(无 race / 顺序差异)
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient

from tests.fixtures._in_memory_repository import InMemoryRepository
from tgmonitor.core.dto import MediaDownloadStatus, ThumbnailDTO
from tgmonitor.core.storage.jsonl_store import JsonlFileStore
from tgmonitor.core.storage.mongo_repo import MongoRepository
from tgmonitor.core.storage.repository import StorageRepository

# ---- fixtures ----


@pytest_asyncio.fixture
async def inmem_repo() -> AsyncIterator[InMemoryRepository]:
    yield InMemoryRepository()


@pytest_asyncio.fixture
async def jsonl_repo(tmp_path) -> AsyncIterator[JsonlFileStore]:
    repo = JsonlFileStore(root=tmp_path / "jsonl")
    await repo.connect()
    try:
        yield repo
    finally:
        await repo.close()


@pytest_asyncio.fixture
async def mongo_repo() -> AsyncIterator[MongoRepository]:
    client = AsyncMongoMockClient()
    repo = MongoRepository.from_client(client, database="tgmonitor_thumb_parity")
    await repo.connect()
    await repo.init_schema()
    try:
        yield repo
    finally:
        await repo.close()


@pytest.fixture(params=["inmem", "jsonl", "mongo"])
def repo(request, inmem_repo, jsonl_repo, mongo_repo) -> StorageRepository:
    """2026-09-29:三后端 parametrize — 所有 thumb 测试统一覆盖。"""
    if request.param == "inmem":
        return inmem_repo
    if request.param == "jsonl":
        return jsonl_repo
    return mongo_repo


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


async def test_save_is_upsert(repo: StorageRepository) -> None:
    """同三元组二次 save 覆盖 — 等价 SQL ON CONFLICT / Mongo replace_one upsert。"""
    await repo.save_thumbnail(_thumb(object_key="thumb/v1.jpg"))
    await repo.save_thumbnail(_thumb(object_key="thumb/v2.jpg"))
    got = await repo.get_thumbnail(100, 1, 0)
    assert got is not None
    assert got.object_key == "thumb/v2.jpg"


async def test_get_missing_returns_none(repo: StorageRepository) -> None:
    """不存在的三元组返 None,不抛。"""
    assert await repo.get_thumbnail(999, 999, 0) is None


async def test_delete_idempotent(repo: StorageRepository) -> None:
    """delete 不存在不抛;已存在 → 真删;再 get 返 None。"""
    # 已存在 → 删
    await repo.save_thumbnail(_thumb())
    await repo.delete_thumbnail(100, 1, 0)
    assert await repo.get_thumbnail(100, 1, 0) is None
    # 不存在 → 不抛
    await repo.delete_thumbnail(999, 999, 0)


async def test_list_thumbnails_for_message_sorted(repo: StorageRepository) -> None:
    """同 msg 多 thumb(media_idx 不同)按 idx ASC 排序。"""
    await repo.save_thumbnail(_thumb(media_idx=2, object_key="thumb/c.jpg"))
    await repo.save_thumbnail(_thumb(media_idx=0, object_key="thumb/a.jpg"))
    await repo.save_thumbnail(_thumb(media_idx=1, object_key="thumb/b.jpg"))
    # 异 msg 不混入
    await repo.save_thumbnail(_thumb(channel_id=200, telegram_msg_id=1, media_idx=0))
    thumbs = await repo.list_thumbnails_for_message(100, 1)
    assert [t.media_idx for t in thumbs] == [0, 1, 2]
    assert [t.object_key for t in thumbs] == [
        "thumb/a.jpg",
        "thumb/b.jpg",
        "thumb/c.jpg",
    ]


async def test_count_by_object_key(repo: StorageRepository) -> None:
    """reconcile_orphans 用:同 object_key 跨 message 出现 N 次 → 计数 N。"""
    await repo.save_thumbnail(
        _thumb(channel_id=1, telegram_msg_id=1, media_idx=0, object_key="thumb/shared.jpg")
    )
    await repo.save_thumbnail(
        _thumb(channel_id=1, telegram_msg_id=2, media_idx=0, object_key="thumb/shared.jpg")
    )
    await repo.save_thumbnail(
        _thumb(channel_id=2, telegram_msg_id=1, media_idx=0, object_key="thumb/other.jpg")
    )
    assert await repo.count_thumbnails_by_object_key("thumb/shared.jpg") == 2
    assert await repo.count_thumbnails_by_object_key("thumb/other.jpg") == 1
    assert await repo.count_thumbnails_by_object_key("thumb/none.jpg") == 0


async def test_delete_message_cascades_thumb(repo: StorageRepository) -> None:
    """2026-09-29:delete_message 同时清掉该消息的所有 thumb(应用级 cascade)。

    PG 还有 FK ON DELETE CASCADE 兜底;Jsonl / Mongo / InMemory 全走
    application-level cascade(无 schema 强约束)。
    """
    # 多 thumb
    await repo.save_thumbnail(_thumb(media_idx=0))
    await repo.save_thumbnail(_thumb(media_idx=1, object_key="thumb/b.jpg"))
    # 异 msg(不该被删)
    await repo.save_thumbnail(_thumb(channel_id=200, telegram_msg_id=1, media_idx=0))
    # 删父 msg
    from datetime import UTC, datetime

    from tgmonitor.core.dto import MessageDTO

    await repo.save_message(
        MessageDTO(
            id=0,
            channel_id=100,
            telegram_msg_id=1,
            date=datetime.now(UTC),
            text="hello",
            media=[],
        )
    )
    await repo.delete_message(100, 1)
    # 100/1 的 thumb 全清
    assert await repo.get_thumbnail(100, 1, 0) is None
    assert await repo.get_thumbnail(100, 1, 1) is None
    # 异 msg 不动
    assert await repo.get_thumbnail(200, 1, 0) is not None
