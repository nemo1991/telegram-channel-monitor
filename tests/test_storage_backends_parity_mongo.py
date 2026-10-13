"""2026-10-12 plan-batch3 目标 3 step 3.6:Mongo 核心 CRUD parity 守门(默认 CI 跑)。

策略:`mongomock_motor` in-process mock(无需 Docker / testcontainers),
默认 CI 矩阵(3 OS × 3.13)都跑。覆盖核心 CRUD 路径,确保 InMemory ↔ Mongo
语义对齐 — 若有「两边都过、单跑挂」的不一致,parity test 先抓。

2026-10-12 删 JSONL 后端后,InMemory + Mongo 是两套真后端(plan-batch2
标记「高风险 跨 DB 差异」),此 parity 守门是删 jsonl 后唯一一道跨 DB
行为对齐防线。

范围:核心 CRUD(频道 / 消息 / 媒体)+ 订阅真理 + save_message 幂等。
不覆盖:`$unwind` / `$match` aggregate pipeline(mongomock 支持度参差,
留 `tests/integration/test_mongo_repo_parity.py` 708 LOC 真集成补)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient

from tests.fixtures._factories import make_message
from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.storage.mongo_repo import MongoRepository

# ---- fixtures ----


@pytest_asyncio.fixture
async def mongo_repo() -> AsyncIterator[MongoRepository]:
    """mongomock in-process Mongo 客户端 — 无需 Docker。"""
    client = AsyncMongoMockClient()
    repo = MongoRepository.from_client(client, database="tgmonitor_test_parity_mongo")
    await repo.connect()
    await repo.init_schema()
    try:
        yield repo
    finally:
        await repo.close()


def _make_channel(cid: int, **kw: object) -> ChannelDTO:
    defaults: dict[str, object] = dict(
        id=cid,
        title=f"Channel {cid}",
        username=f"c{cid}",
        kind="channel",
        is_subscribed=False,
    )
    defaults.update(kw)
    return ChannelDTO(**defaults)  # type: ignore[arg-type]


def _make_message_with_id(cid: int, mid: int, *, text: str = "hi") -> object:
    """工厂 wrapper — 用 conftest 通用 make_message,符合 `id=0` 占位约定。"""
    return make_message(channel_id=cid, msg_id=mid, text=text)


# ---- 频道 CRUD ----


@pytest.mark.asyncio
async def test_upsert_and_get_channel_roundtrip(mongo_repo: MongoRepository) -> None:
    ch = _make_channel(100, title="Test", username="tst", member_count=42)
    await mongo_repo.upsert_channel(ch)
    got = await mongo_repo.get_channel(100)
    assert got is not None
    assert got.id == 100
    assert got.title == "Test"
    assert got.member_count == 42


@pytest.mark.asyncio
async def test_upsert_channel_idempotent(mongo_repo: MongoRepository) -> None:
    """同 id 二次 upsert 不应丢字段(主键语义对齐 PG / InMemory)。"""
    ch1 = _make_channel(100, title="A", member_count=10)
    await mongo_repo.upsert_channel(ch1)
    ch2 = _make_channel(100, title="B", member_count=20)
    await mongo_repo.upsert_channel(ch2)
    got = await mongo_repo.get_channel(100)
    assert got is not None
    assert got.title == "B"
    assert got.member_count == 20


@pytest.mark.asyncio
async def test_list_channels_returns_all(mongo_repo: MongoRepository) -> None:
    for cid in (100, 200, 300):
        await mongo_repo.upsert_channel(_make_channel(cid))
    channels = await mongo_repo.list_channels()
    ids = {c.id for c in channels}
    assert ids == {100, 200, 300}


# ---- 订阅真理 ----


@pytest.mark.asyncio
async def test_set_subscribed_and_list(mongo_repo: MongoRepository) -> None:
    await mongo_repo.upsert_channel(_make_channel(100, is_subscribed=False))
    await mongo_repo.upsert_channel(_make_channel(200, is_subscribed=True))
    await mongo_repo.set_channel_subscribed(100, True)
    subs = await mongo_repo.list_subscribed_channels()
    ids = sorted(c.id for c in subs)
    assert ids == [100, 200]


@pytest.mark.asyncio
async def test_set_subscribed_unsubscribe_roundtrip(mongo_repo: MongoRepository) -> None:
    """subscribe → unsubscribe 路径:list_subscribed 不再含该频道。"""
    await mongo_repo.upsert_channel(_make_channel(100, is_subscribed=True))
    subs_before = await mongo_repo.list_subscribed_channels()
    assert 100 in [c.id for c in subs_before]

    await mongo_repo.set_channel_subscribed(100, False)
    subs_after = await mongo_repo.list_subscribed_channels()
    assert 100 not in [c.id for c in subs_after]


# ---- 消息 CRUD ----


@pytest.mark.asyncio
async def test_save_and_get_message_by_id(mongo_repo: MongoRepository) -> None:
    await mongo_repo.upsert_channel(_make_channel(100))
    m = _make_message_with_id(100, 5001, text="hello")
    new_id = await mongo_repo.save_message(m)
    assert new_id > 0
    got = await mongo_repo.get_message(100, 5001)
    assert got is not None
    assert got.text == "hello"
    assert got.telegram_msg_id == 5001


@pytest.mark.asyncio
async def test_save_message_idempotent(mongo_repo: MongoRepository) -> None:
    """同 (channel_id, telegram_msg_id) 二次 save_message:内容覆盖而非新建。"""
    await mongo_repo.upsert_channel(_make_channel(100))
    m1 = _make_message_with_id(100, 5001, text="old")
    m2 = _make_message_with_id(100, 5001, text="new")
    id1 = await mongo_repo.save_message(m1)
    id2 = await mongo_repo.save_message(m2)
    assert id1 == id2, "upsert 必须复用同一主键,不应分配新 id"
    got = await mongo_repo.get_message(100, 5001)
    assert got is not None
    assert got.text == "new"


@pytest.mark.asyncio
async def test_list_messages_by_channel(mongo_repo: MongoRepository) -> None:
    await mongo_repo.upsert_channel(_make_channel(100))
    for mid in (1, 2, 3):
        await mongo_repo.save_message(_make_message_with_id(100, mid))
    msgs = await mongo_repo.list_messages(channel_ids=[100])
    ids = [m.telegram_msg_id for m in msgs]
    assert sorted(ids) == [1, 2, 3]


# ---- 媒体关联(message.media embedded list,无独立 media save)----


@pytest.mark.asyncio
async def test_save_message_with_media_roundtrip(mongo_repo: MongoRepository) -> None:
    """MediaDTO 随 message.media 一起 save,get_message 读回,media 列表保留。

    2026-10-12 step 3.6:不走 `list_media` 也不走 `find_media_by_file_id`,
    因为两者都依赖 `$unwind` aggregate pipeline,`mongomock_motor` 支持度
    参差(plan 写明)。直接走 `get_message` 验 message.media embedded list
    完整 round-trip。
    """
    from tgmonitor.core.dto import MediaDTO, MediaType

    await mongo_repo.upsert_channel(_make_channel(100))
    photo = MediaDTO(
        type=MediaType.PHOTO,
        telegram_file_id="file_abc",
        file_size=2048,
    )
    m = make_message(channel_id=100, msg_id=5001, text="pic", media=[photo])
    await mongo_repo.save_message(m)

    got = await mongo_repo.get_message(100, 5001)
    assert got is not None
    assert len(got.media) == 1
    media = got.media[0]
    assert media.telegram_file_id == "file_abc"
    assert media.type == MediaType.PHOTO
    assert media.file_size == 2048


@pytest.mark.asyncio
async def test_save_message_two_media_both_preserved(mongo_repo: MongoRepository) -> None:
    """一条消息带 2 个 media 都要保留(media list 顺序对齐)。"""
    from tgmonitor.core.dto import MediaDTO, MediaType

    await mongo_repo.upsert_channel(_make_channel(100))
    media_list = [
        MediaDTO(type=MediaType.PHOTO, telegram_file_id="a"),
        MediaDTO(type=MediaType.DOCUMENT, telegram_file_id="b"),
    ]
    m = make_message(channel_id=100, msg_id=5001, media=media_list)
    await mongo_repo.save_message(m)

    got = await mongo_repo.get_message(100, 5001)
    assert got is not None
    assert len(got.media) == 2
    assert [m_.telegram_file_id for m_ in got.media] == ["a", "b"]
    assert got.media[0].type == MediaType.PHOTO
    assert got.media[1].type == MediaType.DOCUMENT


# ---- 删除 ----


@pytest.mark.asyncio
async def test_delete_message_removes_record(mongo_repo: MongoRepository) -> None:
    await mongo_repo.upsert_channel(_make_channel(100))
    await mongo_repo.save_message(_make_message_with_id(100, 5001))
    await mongo_repo.delete_message(100, 5001)
    got = await mongo_repo.get_message(100, 5001)
    assert got is None


@pytest.mark.asyncio
async def test_delete_channel_cascades_messages(mongo_repo: MongoRepository) -> None:
    """删频道级联清消息(media 是 messages 子文档,无需单独清)。"""
    from tgmonitor.core.dto import MediaDTO, MediaType

    await mongo_repo.upsert_channel(_make_channel(100))
    photo = MediaDTO(type=MediaType.PHOTO, telegram_file_id="f1")
    await mongo_repo.save_message(make_message(channel_id=100, msg_id=5001, media=[photo]))
    await mongo_repo.delete_channel(100)
    assert await mongo_repo.get_message(100, 5001) is None
    # 列表已空(get_message 直接走 channel_id + telegram_msg_id 查询,无 aggregate)
    msgs = await mongo_repo.list_messages(channel_ids=[100])
    assert msgs == []
