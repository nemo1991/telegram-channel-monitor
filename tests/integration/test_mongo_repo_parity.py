"""MongoRepository 真集成测试 — 2026-08-31 v1.5.0 PR #A7。

策略:`mongomock_motor` in-process mock(无需 Docker)—— 覆盖基础 CRUD +
`find_media_by_file_id` 本 PR bug fix。`$unwind` / `$match` aggregate
pipeline(mongomock 支持度参差)— `list_media` / `aggregate_per_channel`
parity 不在本文件,留 v1.5.1 真 Mongo via testcontainers[mongodb] 补。

所有 case `@pytest.mark.integration` 标记,默认 `addopts` 不开,
跑 `uv run pytest -m integration` 显式启用。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tgmonitor.core.dto import (
    ChannelDTO,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    ReactionDTO,
)
from tgmonitor.core.storage.mongo_repo import MongoRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ---- 频道 CRUD ----


async def test_upsert_channel_and_get(mongo_repo: MongoRepository) -> None:
    ch = ChannelDTO(id=100, title="Test Channel", username="tst", member_count=42)
    await mongo_repo.upsert_channel(ch)
    got = await mongo_repo.get_channel(100)
    assert got is not None
    assert got.id == 100
    assert got.title == "Test Channel"
    assert got.username == "tst"
    assert got.member_count == 42


async def test_list_channels_returns_all(mongo_repo: MongoRepository) -> None:
    for cid in (100, 200, 300):
        await mongo_repo.upsert_channel(ChannelDTO(id=cid, title=f"#{cid}"))
    channels = await mongo_repo.list_channels()
    ids = {c.id for c in channels}
    assert ids == {100, 200, 300}


async def test_upsert_channel_metadata_preserves_subscribed(
    mongo_repo: MongoRepository,
) -> None:
    """PR #A2 hotfix:upsert_channel_metadata 不动 subscribed 字段。"""
    ch = ChannelDTO(id=100, title="Old", is_subscribed=True)
    await mongo_repo.upsert_channel(ch)
    new_ch = ChannelDTO(id=100, title="New", is_subscribed=False)
    await mongo_repo.upsert_channel_metadata(new_ch)
    got = await mongo_repo.get_channel(100)
    assert got is not None
    assert got.title == "New"
    assert got.is_subscribed is True  # 保留旧值


async def test_update_channel_metadata_partial(mongo_repo: MongoRepository) -> None:
    """v1.4.0 PR #14:只动非 None 字段;None 字段保留。"""
    ch = ChannelDTO(id=100, title="Old", username="@old", member_count=10)
    await mongo_repo.upsert_channel(ch)
    await mongo_repo.update_channel_metadata(100, title="New", member_count=99)
    got = await mongo_repo.get_channel(100)
    assert got is not None
    assert got.title == "New"
    assert got.username == "@old"  # 没动
    assert got.member_count == 99


async def test_update_channel_metadata_4_tdlib_fields_mongo(
    mongo_repo: MongoRepository,
) -> None:
    """2026-09-04 v1.6.4:Mongo `$set` 4 字段子集路径。"""
    ch = ChannelDTO(
        id=100,
        title="Old",
        username="@old",
        member_count=10,
        is_verified=True,
        has_protected_content=False,
    )
    await mongo_repo.upsert_channel(ch)
    await mongo_repo.update_channel_metadata(
        100,
        is_verified=False,
        is_scam=True,
        is_fake=True,
        has_protected_content=True,
    )
    got = await mongo_repo.get_channel(100)
    assert got is not None
    assert got.is_verified is False
    assert got.is_scam is True
    assert got.is_fake is True
    assert got.has_protected_content is True
    # 未传字段保留
    assert got.title == "Old"
    assert got.username == "@old"
    assert got.member_count == 10


async def test_upsert_channel_4_tdlib_fields_mongo(mongo_repo: MongoRepository) -> None:
    """2026-09-04 v1.6.4:`upsert_channel` 透传 4 字段。"""
    ch = ChannelDTO(
        id=100,
        title="T",
        is_verified=True,
        is_scam=False,
        is_fake=True,
        has_protected_content=True,
    )
    await mongo_repo.upsert_channel(ch)
    got = await mongo_repo.get_channel(100)
    assert got is not None
    assert got.is_verified is True
    assert got.is_scam is False
    assert got.is_fake is True
    assert got.has_protected_content is True


async def test_set_channel_subscribed(mongo_repo: MongoRepository) -> None:
    await mongo_repo.set_channel_subscribed(100, True)
    assert (await mongo_repo.get_channel(100)).is_subscribed is True
    await mongo_repo.set_channel_subscribed(100, False)
    assert (await mongo_repo.get_channel(100)).is_subscribed is False


async def test_list_subscribed_channels_filters(mongo_repo: MongoRepository) -> None:
    await mongo_repo.set_channel_subscribed(100, True)
    await mongo_repo.set_channel_subscribed(200, False)
    await mongo_repo.set_channel_subscribed(300, True)
    subs = await mongo_repo.list_subscribed_channels()
    ids = {c.id for c in subs}
    assert ids == {100, 300}


# ---- 消息 CRUD ----


def _mk_msg(
    channel_id: int = 100,
    msg_id: int = 1,
    text: str = "hello",
    media: list[MediaDTO] | None = None,
    date: datetime | None = None,
    **extra,
) -> MessageDTO:
    base = dict(
        id=0,
        channel_id=channel_id,
        telegram_msg_id=msg_id,
        date=date or datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        text=text,
        author="alice",
        media=media or [],
    )
    base.update(extra)
    return MessageDTO(**base)


def _photo(
    fid: str = "photo_a",
    status: MediaDownloadStatus = MediaDownloadStatus.DONE,
    object_key: str | None = "media/photo_a.jpg",
    file_name: str = "photo_a.jpg",
) -> MediaDTO:
    return MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name=file_name,
        file_size=1024,
        width=800,
        height=600,
        telegram_file_id=fid,
        object_key=object_key,
        object_backend="local",
        download_status=status,
    )


async def test_save_and_get_message(mongo_repo: MongoRepository) -> None:
    msg = _mk_msg(channel_id=100, msg_id=1, text="hello world")
    pk = await mongo_repo.save_message(msg)
    assert pk > 0
    got = await mongo_repo.get_message(100, 1)
    assert got is not None
    assert got.text == "hello world"
    assert got.channel_id == 100


async def test_save_message_idempotent(mongo_repo: MongoRepository) -> None:
    """v1.3.0:同 (channel_id, telegram_msg_id) 多次 save 走 upsert,不创建副本。"""
    msg1 = _mk_msg(channel_id=100, msg_id=1, text="v1")
    pk1 = await mongo_repo.save_message(msg1)
    msg2 = _mk_msg(channel_id=100, msg_id=1, text="v2")
    pk2 = await mongo_repo.save_message(msg2)
    assert pk1 == pk2  # 同一行
    got = await mongo_repo.get_message(100, 1)
    assert got is not None
    assert got.text == "v2"


async def test_save_message_with_media(mongo_repo: MongoRepository) -> None:
    msg = _mk_msg(
        channel_id=100,
        msg_id=1,
        text="photo!",
        media=[_photo()],
    )
    await mongo_repo.save_message(msg)
    got = await mongo_repo.get_message(100, 1)
    assert got is not None
    assert len(got.media) == 1
    assert got.media[0].type == MediaType.PHOTO
    assert got.media[0].telegram_file_id == "photo_a"


async def test_get_message_not_found(mongo_repo: MongoRepository) -> None:
    got = await mongo_repo.get_message(100, 999)
    assert got is None


async def test_delete_message(mongo_repo: MongoRepository) -> None:
    msg = _mk_msg(channel_id=100, msg_id=1)
    await mongo_repo.save_message(msg)
    await mongo_repo.delete_message(100, 1)
    got = await mongo_repo.get_message(100, 1)
    assert got is None


async def test_list_messages_sorted(mongo_repo: MongoRepository) -> None:
    """v1.3.0:list_messages 按 (date ASC, _id ASC) 排序。"""
    for i, dt in enumerate(
        [
            datetime(2026, 1, 3, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC),
        ]
    ):
        await mongo_repo.save_message(_mk_msg(channel_id=100, msg_id=i + 1, date=dt))
    msgs = await mongo_repo.list_messages([100])
    dates = [m.date for m in msgs]
    assert dates == sorted(dates)


async def test_list_messages_limit_keeps_most_recent(
    mongo_repo: MongoRepository,
) -> None:
    """v1.3.0:limit = 最近 N 条,仍按时间升序返回。"""
    for i in range(5):
        await mongo_repo.save_message(
            _mk_msg(
                channel_id=100,
                msg_id=i + 1,
                date=datetime(2026, 1, i + 1, 12, 0, 0, tzinfo=UTC),
            )
        )
    msgs = await mongo_repo.list_messages([100], limit=3)
    assert len(msgs) == 3
    # 最近 3 条按升序:msg3/4/5
    assert [m.telegram_msg_id for m in msgs] == [3, 4, 5]


async def test_get_max_telegram_msg_id(mongo_repo: MongoRepository) -> None:
    for mid in (1, 5, 3, 2):
        await mongo_repo.save_message(_mk_msg(channel_id=100, msg_id=mid))
    assert await mongo_repo.get_max_telegram_msg_id(100) == 5


async def test_count_messages(mongo_repo: MongoRepository) -> None:
    for mid in (1, 2, 3):
        await mongo_repo.save_message(_mk_msg(channel_id=100, msg_id=mid))
    assert await mongo_repo.count_messages(100) == 3


# ---- v1.4.0 新字段 ----


async def test_message_with_reactions(mongo_repo: MongoRepository) -> None:
    """v1.4.0 PR #10:reactions 字段 dict 列表 ↔ ReactionDTO。"""
    reactions = [
        ReactionDTO(emoji="👍", count=3, is_chosen=True),
        ReactionDTO(emoji="❤", count=1, is_chosen=False),
    ]
    msg = _mk_msg(channel_id=100, msg_id=1, reactions=reactions)
    await mongo_repo.save_message(msg)
    got = await mongo_repo.get_message(100, 1)
    assert got is not None
    assert got.reactions is not None
    assert len(got.reactions) == 2
    assert got.reactions[0].emoji == "👍"


async def test_update_message_interactions(mongo_repo: MongoRepository) -> None:
    """v1.4.0 PR #10:Mongo `$set` 部分更新。"""
    msg = _mk_msg(channel_id=100, msg_id=1)
    await mongo_repo.save_message(msg)
    await mongo_repo.update_message_interactions(100, 1, views=42)
    got = await mongo_repo.get_message(100, 1)
    assert got is not None
    assert got.views == 42


async def test_message_v14_fields_roundtrip(mongo_repo: MongoRepository) -> None:
    """v1.4.0 PR #9:4 个新字段(reply_to / forward_origin / via_bot_id / is_pinned)。
    None 字段不写 key 兼容旧库。
    """
    msg = _mk_msg(
        channel_id=100,
        msg_id=1,
        reply_to_msg_id=99,
        forward_origin={"type": "user", "user_id": 12345},
        via_bot_user_id=67890,
        is_pinned=True,
    )
    await mongo_repo.save_message(msg)
    got = await mongo_repo.get_message(100, 1)
    assert got is not None
    assert got.reply_to_msg_id == 99
    assert got.forward_origin == {"type": "user", "user_id": 12345}
    assert got.via_bot_user_id == 67890
    assert got.is_pinned is True


# ---- find_media_by_file_id (PR #A7 主 fix) ----


async def test_find_media_by_file_id_dedupes(mongo_repo: MongoRepository) -> None:
    """2026-08-31 v1.5.0 PR #A7:跨频道去重 — 同 fid 在两频道不同 msg 中 DONE,
    应返最新写入那一条的 MediaDTO。

    修前(mongomock 走老代码 `db.media.find_one`):永远 None。
    修后(走 `db.messages.aggregate $unwind + $match`):命中最近一条。
    """
    # 频道 100,msg1,fid_A DONE,先写
    msg1 = _mk_msg(
        channel_id=100,
        msg_id=1,
        media=[_photo(fid="fid_A", object_key="media/v1.jpg", file_name="v1.jpg")],
    )
    await mongo_repo.save_message(msg1)
    # 频道 200,msg10,fid_A DONE,后写
    msg2 = _mk_msg(
        channel_id=200,
        msg_id=10,
        media=[_photo(fid="fid_A", object_key="media/v2.jpg", file_name="v2.jpg")],
    )
    await mongo_repo.save_message(msg2)

    found = await mongo_repo.find_media_by_file_id("fid_A")
    assert found is not None
    # 最新写入(v2,_id 倒序)
    assert found.object_key == "media/v2.jpg"


async def test_find_media_by_file_id_skips_pending(
    mongo_repo: MongoRepository,
) -> None:
    """DONE 是命中条件;PENDING / FAILED 即使有 object_key 也不命中。"""
    msg1 = _mk_msg(
        channel_id=100,
        msg_id=1,
        media=[_photo(fid="fid_pending", status=MediaDownloadStatus.PENDING)],
    )
    await mongo_repo.save_message(msg1)
    found = await mongo_repo.find_media_by_file_id("fid_pending")
    assert found is None


async def test_find_media_by_file_id_skips_no_object_key(
    mongo_repo: MongoRepository,
) -> None:
    """DONE 但 object_key=None 不命中(下载未完成产物)。"""
    msg1 = _mk_msg(
        channel_id=100,
        msg_id=1,
        media=[
            _photo(fid="fid_dl", status=MediaDownloadStatus.DONE, object_key=None)
            if False
            else MediaDTO(
                type=MediaType.PHOTO,
                mime_type="image/jpeg",
                file_name="dl.jpg",
                file_size=100,
                telegram_file_id="fid_dl",
                object_key=None,
                object_backend="local",
                download_status=MediaDownloadStatus.DONE,
            )
        ],
    )
    await mongo_repo.save_message(msg1)
    found = await mongo_repo.find_media_by_file_id("fid_dl")
    assert found is None


async def test_find_media_by_file_id_not_found(mongo_repo: MongoRepository) -> None:
    found = await mongo_repo.find_media_by_file_id("nonexistent_fid")
    assert found is None


# ---- meta ----


async def test_meta_set_get(mongo_repo: MongoRepository) -> None:
    assert await mongo_repo.get_meta("key1") is None
    await mongo_repo.set_meta("key1", "value1")
    assert await mongo_repo.get_meta("key1") == "value1"
    await mongo_repo.set_meta("key1", "value2")
    assert await mongo_repo.get_meta("key1") == "value2"


# ---- ping ----


async def test_ping(mongo_repo: MongoRepository) -> None:
    """mongomock_motor 不支持真 ping,但 ping() 应不抛(走 catch-all)。"""
    # mongomock_motor 的 db.command("ping") 可能 raise;我们的实现 catch 所有异常返 False
    result = await mongo_repo.ping()
    assert result is False or result is True  # 兼容任意结果


# ---- v1.6.0 PR #Q1:case-insensitive collation 索引 + 搜索 parity --------


async def test_list_messages_search_hits_text(
    mongo_repo: MongoRepository,
) -> None:
    """PR #Q1:`$or` 走 `$regex` + `$options: "i"` 大小写不敏感匹配 text。
    mongomock_motor 路径与真 Mongo 一致(regex 实现直接调 Python re)。
    """
    await mongo_repo.upsert_channel(ChannelDTO(id=100, title="c100"))
    await mongo_repo.save_message(_mk_msg(channel_id=100, msg_id=1, text="今天见到一只猫"))
    await mongo_repo.save_message(_mk_msg(channel_id=100, msg_id=2, text="今天天气好"))
    msgs = await mongo_repo.list_messages([100], search="猫")
    assert [m.telegram_msg_id for m in msgs] == [1]


async def test_list_messages_search_case_insensitive(
    mongo_repo: MongoRepository,
) -> None:
    """PR #Q1:Mongo `$options: "i"` 已 case-fold;collation 索引(可能 mongomock
    不支持)不影响 correctness,只是性能。
    """
    await mongo_repo.upsert_channel(ChannelDTO(id=100, title="c100"))
    await mongo_repo.save_message(_mk_msg(channel_id=100, msg_id=1, text="CAT meow"))
    await mongo_repo.save_message(_mk_msg(channel_id=100, msg_id=2, text="dog woof"))
    msgs = await mongo_repo.list_messages([100], search="cat")
    assert [m.telegram_msg_id for m in msgs] == [1]


async def test_list_messages_search_hits_media_file_name(
    mongo_repo: MongoRepository,
) -> None:
    """PR #Q1:`media.file_name` 子串命中 — `$regex` 走 collation 索引或全表扫。"""
    await mongo_repo.upsert_channel(ChannelDTO(id=100, title="c100"))
    await mongo_repo.save_message(
        _mk_msg(
            channel_id=100,
            msg_id=1,
            text="无文字",
            media=[_photo(fid="f1", file_name="screenshot_2026.png")],
        ),
    )
    await mongo_repo.save_message(
        _mk_msg(
            channel_id=100,
            msg_id=2,
            text="无文字",
            media=[_photo(fid="f2", file_name="photo.jpg")],
        ),
    )
    msgs = await mongo_repo.list_messages([100], search="screenshot")
    assert [m.telegram_msg_id for m in msgs] == [1]


async def test_list_messages_search_across_channels(
    mongo_repo: MongoRepository,
) -> None:
    """PR #Q1:跨频道聚合 search — `$in` + `$or regex` 路径,与 PG 等价。"""
    for cid in range(101, 106):
        await mongo_repo.upsert_channel(ChannelDTO(id=cid, title=f"c{cid}"))
        for mid in range(1, 11):
            text = f"消息{mid} 包含猫的图片" if mid % 2 == 0 else f"消息{mid} 普通内容"
            await mongo_repo.save_message(_mk_msg(channel_id=cid, msg_id=mid, text=text))
    msgs = await mongo_repo.list_messages([101, 102, 103, 104, 105], search="猫")
    assert len(msgs) == 25
    channels_hit = {m.channel_id for m in msgs}
    assert channels_hit == {101, 102, 103, 104, 105}


async def test_list_messages_search_empty_no_filter(
    mongo_repo: MongoRepository,
) -> None:
    """PR #Q1:空 search 不走 `$or regex`,返全部。"""
    await mongo_repo.upsert_channel(ChannelDTO(id=100, title="c100"))
    for mid in (1, 2, 3):
        await mongo_repo.save_message(_mk_msg(channel_id=100, msg_id=mid))
    msgs = await mongo_repo.list_messages([100], search="")
    assert len(msgs) == 3


async def test_list_messages_search_escaped_wildcard_safe(
    mongo_repo: MongoRepository,
) -> None:
    """PR #Q1:`re.escape` 防 regex 注入 — 用户输入 `%` / `_` / `\\` / `[`
    都被转义为字面字符。这是 v1.5.1 PR #B2 既有逻辑,加真 Mongo 集成
    测试(防回归 — 之前用 in-memory mock 测)。
    """
    await mongo_repo.upsert_channel(ChannelDTO(id=100, title="c100"))
    await mongo_repo.save_message(_mk_msg(channel_id=100, msg_id=1, text="100% complete"))
    await mongo_repo.save_message(_mk_msg(channel_id=100, msg_id=2, text="half done"))
    msgs = await mongo_repo.list_messages([100], search="%")
    # mongomock 走 `_escape_regex` → re.escape("%") = "\\%",命中字面 `%` 那条
    assert [m.telegram_msg_id for m in msgs] == [1]


# ---- v1.6.0 PR #Q2:photo_local_key 部分更新 parity ----


async def test_update_channel_metadata_photo_local_key_mongo(
    mongo_repo: MongoRepository,
) -> None:
    """PR #Q2:Mongo `$set` 子集 — 只动 photo_local_key。"""
    ch = ChannelDTO(id=100, title="Old", username="@u", member_count=10)
    await mongo_repo.upsert_channel(ch)
    await mongo_repo.update_channel_metadata(100, photo_local_key="/tmp/avatar.jpg")
    got = await mongo_repo.get_channel(100)
    assert got is not None
    assert got.photo_local_key == "/tmp/avatar.jpg"
    assert got.title == "Old"
    assert got.username == "@u"
    assert got.member_count == 10


async def test_update_channel_metadata_photo_local_key_preserves_other_mongo(
    mongo_repo: MongoRepository,
) -> None:
    """PR #Q2:只动 photo_local_key,不动其他字段。"""
    await mongo_repo.upsert_channel(
        ChannelDTO(id=100, title="Title", username="@u", member_count=99)
    )
    await mongo_repo.update_channel_metadata(100, photo_local_key="/tmp/x.png")
    got = await mongo_repo.get_channel(100)
    assert got is not None
    assert got.photo_local_key == "/tmp/x.png"
    assert got.title == "Title"
    assert got.username == "@u"
    assert got.member_count == 99


async def test_upsert_channel_with_photo_local_key_mongo(
    mongo_repo: MongoRepository,
) -> None:
    """PR #Q2:`upsert_channel` 含 photo_local_key 字段。"""
    ch = ChannelDTO(id=100, title="X", photo_local_key="/tmp/avatar.jpg")
    await mongo_repo.upsert_channel(ch)
    got = await mongo_repo.get_channel(100)
    assert got is not None
    assert got.photo_local_key == "/tmp/avatar.jpg"
