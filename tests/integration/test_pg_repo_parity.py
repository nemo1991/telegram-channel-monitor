"""PostgresRepository 真集成测试 — 2026-08-31 v1.5.0 PR #A7。

策略:testcontainers 启动真 PG 服务(`postgres:16-alpine` image),跑 asyncpg
真 SQL,验证 `schema.sql` 迁移路径 / init_schema / JSONB 序列化等。

无 Docker 环境(本地 dev / sandbox):`pg_engine` fixture 自动 skip,
`uv run pytest -m integration` 不会因缺 Docker 整链路炸。

所有 case `@pytest.mark.integration` 标记,默认 `addopts` 不开,
跑 `uv run pytest -m integration` 显式启用。CI integration job 自动跑。
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
from tgmonitor.core.storage.postgres_repo import PostgresRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


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


# ---- 频道 CRUD ----


async def test_upsert_channel_and_get(pg_repo: PostgresRepository) -> None:
    ch = ChannelDTO(id=100, title="Test", username="tst", member_count=42)
    await pg_repo.upsert_channel(ch)
    got = await pg_repo.get_channel(100)
    assert got is not None
    assert got.id == 100
    assert got.title == "Test"
    assert got.member_count == 42


async def test_upsert_channel_metadata_preserves_subscribed(
    pg_repo: PostgresRepository,
) -> None:
    ch = ChannelDTO(id=100, title="Old", is_subscribed=True)
    await pg_repo.upsert_channel(ch)
    await pg_repo.upsert_channel_metadata(ChannelDTO(id=100, title="New", is_subscribed=False))
    got = await pg_repo.get_channel(100)
    assert got is not None
    assert got.title == "New"
    assert got.is_subscribed is True  # 保留


async def test_update_channel_metadata_partial(pg_repo: PostgresRepository) -> None:
    ch = ChannelDTO(id=100, title="Old", username="@old", member_count=10)
    await pg_repo.upsert_channel(ch)
    await pg_repo.update_channel_metadata(100, title="New", member_count=99)
    got = await pg_repo.get_channel(100)
    assert got is not None
    assert got.title == "New"
    assert got.username == "@old"
    assert got.member_count == 99


async def test_update_channel_metadata_4_tdlib_fields_pg(pg_repo: PostgresRepository) -> None:
    """2026-09-04 v1.6.4:4 个 spammer 字段(verified/scam/fake/has_protected_content)
    COALESCE partial update 路径。

    真 PG 验证 ALTER TABLE IF NOT EXISTS 迁移 + $1-$8 COALESCE 占位。
    """
    ch = ChannelDTO(
        id=100,
        title="Old",
        username="@old",
        member_count=10,
        is_verified=True,
    )
    await pg_repo.upsert_channel(ch)
    # 部分更新 4 字段
    await pg_repo.update_channel_metadata(
        100,
        is_verified=False,
        is_scam=True,
        is_fake=True,
        has_protected_content=True,
    )
    got = await pg_repo.get_channel(100)
    assert got is not None
    assert got.is_verified is False  # 显式 False 覆盖
    assert got.is_scam is True
    assert got.is_fake is True
    assert got.has_protected_content is True
    # 未传字段保留
    assert got.title == "Old"
    assert got.username == "@old"
    assert got.member_count == 10


async def test_upsert_channel_4_tdlib_fields_pg(pg_repo: PostgresRepository) -> None:
    """2026-09-04 v1.6.4:`upsert_channel` INSERT + ON CONFLICT 4 字段路径。"""
    ch = ChannelDTO(
        id=100,
        title="T",
        is_verified=True,
        is_scam=False,
        is_fake=True,
        has_protected_content=True,
    )
    await pg_repo.upsert_channel(ch)
    got = await pg_repo.get_channel(100)
    assert got is not None
    assert got.is_verified is True
    assert got.is_scam is False
    assert got.is_fake is True
    assert got.has_protected_content is True


async def test_list_subscribed_channels(pg_repo: PostgresRepository) -> None:
    await pg_repo.set_channel_subscribed(100, True)
    await pg_repo.set_channel_subscribed(200, False)
    await pg_repo.set_channel_subscribed(300, True)
    subs = await pg_repo.list_subscribed_channels()
    assert {c.id for c in subs} == {100, 300}


async def test_delete_channel_cascades(pg_repo: PostgresRepository) -> None:
    await pg_repo.upsert_channel(ChannelDTO(id=100, title="X"))
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=1))
    await pg_repo.delete_channel(100)
    assert await pg_repo.get_channel(100) is None
    assert await pg_repo.get_message(100, 1) is None


# ---- 消息 CRUD ----


async def test_save_and_get_message(pg_repo: PostgresRepository) -> None:
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=1, text="hi"))
    got = await pg_repo.get_message(100, 1)
    assert got is not None
    assert got.text == "hi"


async def test_save_message_idempotent(pg_repo: PostgresRepository) -> None:
    pk1 = await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=1, text="v1"))
    pk2 = await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=1, text="v2"))
    assert pk1 == pk2
    got = await pg_repo.get_message(100, 1)
    assert got is not None
    assert got.text == "v2"


async def test_save_message_with_media(pg_repo: PostgresRepository) -> None:
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=1, media=[_photo()]))
    got = await pg_repo.get_message(100, 1)
    assert got is not None
    assert len(got.media) == 1
    assert got.media[0].type == MediaType.PHOTO


async def test_delete_message(pg_repo: PostgresRepository) -> None:
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=1))
    await pg_repo.delete_message(100, 1)
    assert await pg_repo.get_message(100, 1) is None


async def test_list_messages_sorted_ascending(pg_repo: PostgresRepository) -> None:
    for i, dt in enumerate(
        [
            datetime(2026, 1, 3, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC),
        ]
    ):
        await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=i + 1, date=dt))
    msgs = await pg_repo.list_messages([100])
    assert [m.date for m in msgs] == sorted([m.date for m in msgs])


async def test_list_messages_limit_keeps_most_recent(
    pg_repo: PostgresRepository,
) -> None:
    for i in range(5):
        await pg_repo.save_message(
            _mk_msg(
                channel_id=100,
                msg_id=i + 1,
                date=datetime(2026, 1, i + 1, 12, 0, 0, tzinfo=UTC),
            )
        )
    msgs = await pg_repo.list_messages([100], limit=3)
    assert [m.telegram_msg_id for m in msgs] == [3, 4, 5]


async def test_get_max_telegram_msg_id(pg_repo: PostgresRepository) -> None:
    for mid in (1, 5, 3, 2):
        await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=mid))
    assert await pg_repo.get_max_telegram_msg_id(100) == 5


async def test_count_messages(pg_repo: PostgresRepository) -> None:
    for mid in (1, 2, 3):
        await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=mid))
    assert await pg_repo.count_messages(100) == 3


# ---- v1.4.0 新字段 ----


async def test_message_v14_fields_roundtrip(pg_repo: PostgresRepository) -> None:
    """v1.4.0 PR #9:reply_to / forward_origin / via_bot_id / is_pinned 持久化。"""
    msg = _mk_msg(
        channel_id=100,
        msg_id=1,
        reply_to_msg_id=99,
        forward_origin={"type": "user", "user_id": 12345},
        via_bot_user_id=67890,
        is_pinned=True,
    )
    await pg_repo.save_message(msg)
    got = await pg_repo.get_message(100, 1)
    assert got is not None
    assert got.reply_to_msg_id == 99
    assert got.forward_origin == {"type": "user", "user_id": 12345}
    assert got.via_bot_user_id == 67890
    assert got.is_pinned is True


async def test_message_with_reactions(pg_repo: PostgresRepository) -> None:
    """v1.4.0 PR #10:reactions ↔ JSON 字段。"""
    msg = _mk_msg(
        channel_id=100,
        msg_id=1,
        reactions=[ReactionDTO(emoji="👍", count=3, is_chosen=True)],
    )
    await pg_repo.save_message(msg)
    got = await pg_repo.get_message(100, 1)
    assert got is not None
    assert got.reactions is not None
    assert len(got.reactions) == 1
    assert got.reactions[0].emoji == "👍"


async def test_update_message_interactions(pg_repo: PostgresRepository) -> None:
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=1))
    await pg_repo.update_message_interactions(100, 1, views=42)
    got = await pg_repo.get_message(100, 1)
    assert got is not None
    assert got.views == 42


# ---- find_media_by_file_id (PG 此前正确,本 PR parity 验证保留行为) ----


async def test_find_media_by_file_id_dedupes(pg_repo: PostgresRepository) -> None:
    msg1 = _mk_msg(
        channel_id=100,
        msg_id=1,
        media=[_photo(fid="fid_A", object_key="media/v1.jpg", file_name="v1.jpg")],
    )
    await pg_repo.save_message(msg1)
    msg2 = _mk_msg(
        channel_id=200,
        msg_id=10,
        media=[_photo(fid="fid_A", object_key="media/v2.jpg", file_name="v2.jpg")],
    )
    await pg_repo.save_message(msg2)
    found = await pg_repo.find_media_by_file_id("fid_A")
    assert found is not None
    assert found.object_key == "media/v2.jpg"  # 最新


async def test_find_media_by_file_id_skips_pending(
    pg_repo: PostgresRepository,
) -> None:
    await pg_repo.save_message(
        _mk_msg(
            channel_id=100,
            msg_id=1,
            media=[_photo(fid="fid_pending", status=MediaDownloadStatus.PENDING)],
        )
    )
    assert await pg_repo.find_media_by_file_id("fid_pending") is None


async def test_find_media_by_file_id_not_found(
    pg_repo: PostgresRepository,
) -> None:
    assert await pg_repo.find_media_by_file_id("nonexistent") is None


# ---- meta ----


async def test_meta_set_get(pg_repo: PostgresRepository) -> None:
    assert await pg_repo.get_meta("k1") is None
    await pg_repo.set_meta("k1", "v1")
    assert await pg_repo.get_meta("k1") == "v1"


# ---- ping ----


async def test_ping(pg_repo: PostgresRepository) -> None:
    """真 PG ping 应返 True。"""
    assert await pg_repo.ping() is True


# ---- v1.6.0 PR #Q1:pg_trgm GIN 索引 + 搜索 parity -----------------------


async def test_list_messages_search_hits_text(
    pg_repo: PostgresRepository,
) -> None:
    """PR #Q1:`LOWER(text) LIKE` 命中 pg_trgm GIN 索引(走 planner 自动命中)。

    验证搜索功能正确:4 条种子数据,search="猫" 只命中 msg1(text 含「猫」)。
    """
    await pg_repo.upsert_channel(ChannelDTO(id=100, title="c100"))
    await pg_repo.save_message(
        _mk_msg(channel_id=100, msg_id=1, text="今天见到一只猫"),
    )
    await pg_repo.save_message(
        _mk_msg(channel_id=100, msg_id=2, text="今天天气好"),
    )
    msgs = await pg_repo.list_messages([100], search="猫")
    assert [m.telegram_msg_id for m in msgs] == [1]


async def test_list_messages_search_case_insensitive(
    pg_repo: PostgresRepository,
) -> None:
    """PR #Q1:大小写不敏感命中 — schema.sql 索引是 `LOWER(text) gin_trgm_ops`,
    SQL 端 `LOWER(text) LIKE` 自动 case-fold 命中。
    """
    await pg_repo.upsert_channel(ChannelDTO(id=100, title="c100"))
    await pg_repo.save_message(
        _mk_msg(channel_id=100, msg_id=1, text="CAT meow"),
    )
    await pg_repo.save_message(
        _mk_msg(channel_id=100, msg_id=2, text="dog woof"),
    )
    msgs = await pg_repo.list_messages([100], search="cat")
    assert [m.telegram_msg_id for m in msgs] == [1]
    # 反向大小写也命中(索引已 case-fold,无需重建)
    msgs2 = await pg_repo.list_messages([100], search="CAT")
    assert [m.telegram_msg_id for m in msgs2] == [1]


async def test_list_messages_search_hits_media_file_name(
    pg_repo: PostgresRepository,
) -> None:
    """PR #Q1:`EXISTS (SELECT 1 FROM media ... LOWER(file_name) LIKE ...)` 命中
    `idx_media_file_name_trgm` GIN 索引 — 子串搜媒体文件名。
    """
    await pg_repo.upsert_channel(ChannelDTO(id=100, title="c100"))
    await pg_repo.save_message(
        _mk_msg(
            channel_id=100,
            msg_id=1,
            text="无文字",
            media=[_photo(fid="f1", file_name="screenshot_2026.png")],
        ),
    )
    await pg_repo.save_message(
        _mk_msg(
            channel_id=100,
            msg_id=2,
            text="无文字",
            media=[_photo(fid="f2", file_name="photo.jpg")],
        ),
    )
    msgs = await pg_repo.list_messages([100], search="screenshot")
    assert [m.telegram_msg_id for m in msgs] == [1]


async def test_list_messages_search_across_channels(
    pg_repo: PostgresRepository,
) -> None:
    """PR #Q1:跨频道聚合 search(v1.5.3 PR #D2 引入的聚合 scope)。
    5 频道各 10 消息,搜「猫」应横跨 5 频道命中所有含「猫」消息。
    """
    for cid in range(101, 106):
        await pg_repo.upsert_channel(ChannelDTO(id=cid, title=f"c{cid}"))
        for mid in range(1, 11):
            text = f"消息{mid} 包含猫的图片" if mid % 2 == 0 else f"消息{mid} 普通内容"
            await pg_repo.save_message(_mk_msg(channel_id=cid, msg_id=mid, text=text))
    msgs = await pg_repo.list_messages([101, 102, 103, 104, 105], search="猫")
    # 每频道 mid=2,4,6,8,10 共 5 条 × 5 频道 = 25 条
    assert len(msgs) == 25
    channels_hit = {m.channel_id for m in msgs}
    assert channels_hit == {101, 102, 103, 104, 105}


async def test_list_messages_search_empty_no_filter(
    pg_repo: PostgresRepository,
) -> None:
    """PR #Q1:`search=""` 不走 LIKE 子句,返全部。
    验证空 search 不命中任何 LIKE path(planner 短路)。
    """
    await pg_repo.upsert_channel(ChannelDTO(id=100, title="c100"))
    for mid in (1, 2, 3):
        await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=mid))
    msgs = await pg_repo.list_messages([100], search="")
    assert len(msgs) == 3


async def test_list_messages_search_escaped_wildcard_safe(
    pg_repo: PostgresRepository,
) -> None:
    """PR #Q1:user 输入 `%` 不应被当作 SQL 通配符 — LIKE ESCAPE '\\'
    把 `%` 转义为字面字符。这是 v1.5.1 PR #B2 既有逻辑,本 PR 加真 PG
    集成测试兜底(防回归 — 之前只用 in-memory mock 测)。
    """
    await pg_repo.upsert_channel(ChannelDTO(id=100, title="c100"))
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=1, text="100% complete"))
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=2, text="half done"))
    # 搜 `%` 应只命中字面 % 的那条,不是所有
    msgs = await pg_repo.list_messages([100], search="%")
    assert [m.telegram_msg_id for m in msgs] == [1]


async def test_init_schema_pg_trgm_extension_idempotent(
    pg_repo: PostgresRepository,
) -> None:
    """PR #Q1:`CREATE EXTENSION IF NOT EXISTS pg_trgm` 幂等 — 二次跑
    init_schema 不抛错,且 pg_trgm extension 存在。
    """
    # pg_repo fixture 已经跑过一次 init_schema;再跑一次验证幂等
    await pg_repo.init_schema()
    assert await pg_repo.ping() is True
    # 验证 extension 真的装上了
    async with pg_repo._pool.acquire() as conn:  # type: ignore[attr-defined]
        ext_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm')"
        )
    assert ext_exists is True


# ---- v1.6.0 PR #Q2:photo_local_key 部分更新 parity ----


async def test_update_channel_metadata_photo_local_key_pg(
    pg_repo: PostgresRepository,
) -> None:
    """PR #Q2:`COALESCE($4, photo_local_key)` 让 None 不动、其他字段保留。"""
    ch = ChannelDTO(id=100, title="Old", username="@u", member_count=10)
    await pg_repo.upsert_channel(ch)
    await pg_repo.update_channel_metadata(100, photo_local_key="/tmp/avatar.jpg")
    got = await pg_repo.get_channel(100)
    assert got is not None
    assert got.photo_local_key == "/tmp/avatar.jpg"
    assert got.title == "Old"
    assert got.username == "@u"
    assert got.member_count == 10


async def test_update_channel_metadata_photo_local_key_preserves_other_pg(
    pg_repo: PostgresRepository,
) -> None:
    """PR #Q2:只动 photo_local_key,不动其他字段。"""
    await pg_repo.upsert_channel(ChannelDTO(id=100, title="Title", username="@u", member_count=99))
    await pg_repo.update_channel_metadata(100, photo_local_key="/tmp/x.png")
    got = await pg_repo.get_channel(100)
    assert got is not None
    assert got.photo_local_key == "/tmp/x.png"
    assert got.title == "Title"
    assert got.username == "@u"
    assert got.member_count == 99


async def test_upsert_channel_with_photo_local_key_pg(
    pg_repo: PostgresRepository,
) -> None:
    """PR #Q2:`upsert_channel` 全字段 upsert 含 photo_local_key。"""
    ch = ChannelDTO(id=100, title="X", photo_local_key="/tmp/avatar.jpg")
    await pg_repo.upsert_channel(ch)
    got = await pg_repo.get_channel(100)
    assert got is not None
    assert got.photo_local_key == "/tmp/avatar.jpg"


# ---- schema 幂等性 ----


async def test_init_schema_idempotent(pg_repo: PostgresRepository) -> None:
    """`init_schema` 多次调用不报错(DDL IF NOT EXISTS 幂等)。"""
    await pg_repo.init_schema()
    await pg_repo.init_schema()
    # 仍可正常使用
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=1))


# ---- ON DELETE CASCADE ----


async def test_delete_message_cascades_media(pg_repo: PostgresRepository) -> None:
    """ON DELETE CASCADE:删 messages 行时关联 media 自动删。"""
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=1, media=[_photo()]))
    # 直接走 SQL 数 media 行
    async with pg_repo._pool.acquire() as conn:  # type: ignore[attr-defined]
        before = await conn.fetchval("SELECT count(*) FROM media")
        await pg_repo.delete_message(100, 1)
        after = await conn.fetchval("SELECT count(*) FROM media")
    assert before == 1
    assert after == 0


# ---- 2026-09-08 v1.7.0:批量 delete_messages parity ----


async def test_delete_messages_batch_pg(pg_repo: PostgresRepository) -> None:
    """批量删 — channel_id=100 的 3 条消息全部消失。"""
    for mid in (10, 11, 12):
        await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=mid))
    # 验证已存
    for mid in (10, 11, 12):
        m = await pg_repo.get_message(100, mid)
        assert m is not None
    # 批量删
    await pg_repo.delete_messages(100, [10, 11, 12])
    # 全没了
    for mid in (10, 11, 12):
        m = await pg_repo.get_message(100, mid)
        assert m is None


async def test_delete_messages_partial(pg_repo: PostgresRepository) -> None:
    """批量删 — 只删列表里的消息,不影响其他。"""
    for mid in (20, 21, 22, 23, 24):
        await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=mid))
    await pg_repo.delete_messages(100, [20, 22, 24])
    for mid in (20, 22, 24):
        assert await pg_repo.get_message(100, mid) is None
    # 21, 23 保留
    for mid in (21, 23):
        assert await pg_repo.get_message(100, mid) is not None


async def test_delete_messages_empty_list_noop(pg_repo: PostgresRepository) -> None:
    """空 msg_ids 列表 → no-op,所有现存消息保留。"""
    for mid in (30, 31):
        await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=mid))
    await pg_repo.delete_messages(100, [])
    for mid in (30, 31):
        assert await pg_repo.get_message(100, mid) is not None


async def test_delete_messages_nonexistent_idempotent(pg_repo: PostgresRepository) -> None:
    """批量删不存在的 msg_ids → 不抛(idempotent)。"""
    await pg_repo.delete_messages(100, [999, 1000])
    # 不抛即通过


async def test_delete_messages_cascades_media(pg_repo: PostgresRepository) -> None:
    """批量删 — 关联 media 自动 CASCADE 删除。"""
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=40, media=[_photo(fid="f40_a")]))
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=41, media=[_photo(fid="f41_a")]))
    async with pg_repo._pool.acquire() as conn:  # type: ignore[attr-defined]
        before = await conn.fetchval("SELECT count(*) FROM media")
    assert before == 2
    await pg_repo.delete_messages(100, [40, 41])
    async with pg_repo._pool.acquire() as conn:  # type: ignore[attr-defined]
        after = await conn.fetchval("SELECT count(*) FROM media")
    assert after == 0


async def test_delete_messages_isolates_channels(pg_repo: PostgresRepository) -> None:
    """批量删 — 只影响指定 channel_id,不影响其它频道的消息。"""
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=50))
    await pg_repo.save_message(_mk_msg(channel_id=200, msg_id=50))  # 不同频道同 msg_id
    await pg_repo.delete_messages(100, [50])
    assert await pg_repo.get_message(100, 50) is None
    assert await pg_repo.get_message(200, 50) is not None


# ============== 2026-09-09 v1.7.2:用户元数据 parity ==============


async def test_set_favorite_pg(pg_repo: PostgresRepository) -> None:
    """set_favorite(True) → get_message 读回的 is_favorite=True。"""
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=10))
    await pg_repo.set_favorite(100, 10, True)
    m = await pg_repo.get_message(100, 10)
    assert m is not None
    assert m.is_favorite is True


async def test_set_favorite_toggle_back_pg(pg_repo: PostgresRepository) -> None:
    """set_favorite(True → False) → 状态可逆(LIVE 取消收藏)。"""
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=10, is_favorite=True))
    assert (await pg_repo.get_message(100, 10)).is_favorite is True  # type: ignore[union-attr]
    await pg_repo.set_favorite(100, 10, False)
    assert (await pg_repo.get_message(100, 10)).is_favorite is False  # type: ignore[union-attr]


async def test_set_tags_pg(pg_repo: PostgresRepository) -> None:
    """set_tags([tech, news]) → tags 列表原样读回(顺序保留)。"""
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=10))
    await pg_repo.set_tags(100, 10, ["tech", "news"])
    m = await pg_repo.get_message(100, 10)
    assert m is not None
    assert m.tags == ["tech", "news"]


async def test_set_notes_pg(pg_repo: PostgresRepository) -> None:
    """set_notes('后续 review') → notes 文本读回。"""
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=10))
    await pg_repo.set_notes(100, 10, "后续 review")
    m = await pg_repo.get_message(100, 10)
    assert m is not None
    assert m.notes == "后续 review"


async def test_list_favorites_pg(pg_repo: PostgresRepository) -> None:
    """list_favorites → 只含 is_favorite=True 的消息。"""
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=10, is_favorite=True))
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=11, is_favorite=False))
    await pg_repo.save_message(_mk_msg(channel_id=200, msg_id=10, is_favorite=True))
    favs = await pg_repo.list_favorites()
    fav_ids = {(m.channel_id, m.telegram_msg_id) for m in favs}
    assert (100, 10) in fav_ids
    assert (200, 10) in fav_ids
    assert (100, 11) not in fav_ids


async def test_list_by_tag_pg(pg_repo: PostgresRepository) -> None:
    """list_by_tag('tech') → 含 'tech' 标签的全部消息。"""
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=10, tags=["tech", "news"]))
    await pg_repo.save_message(_mk_msg(channel_id=100, msg_id=11, tags=["music"]))
    await pg_repo.save_message(_mk_msg(channel_id=200, msg_id=10, tags=["tech"]))
    hits = await pg_repo.list_by_tag("tech")
    hit_ids = {(m.channel_id, m.telegram_msg_id) for m in hits}
    assert (100, 10) in hit_ids
    assert (200, 10) in hit_ids
    assert (100, 11) not in hit_ids


async def test_metadata_schema_migration_preserves_old_rows_pg(
    pg_repo: PostgresRepository,
) -> None:
    """2026-09-09 v1.7.2:schema 迁移 — 旧行(没 is_favorite/tags/notes)读回默认值。

    模拟「ALTER TABLE 已跑过,数据是迁移前写入」的边界:
    直接 SQL 插一条没新列的行,然后验证 .get() fallback 兜底。

    PG 9.6+ ADD COLUMN IF NOT EXISTS 有非空默认时会回填,故「旧行」要绕过:
    - 用 SET DEFAULT FALSE 后插的旧行会带默认值 — 测的是 _row_to_message 读路径
      对 NULL 字段用 .get(..., default) 的兜底,需要显式不依赖 schema default。
    """
    async with pg_repo._pool.acquire() as conn:  # type: ignore[attr-defined]
        # 删 NOT NULL DEFAULT 限制,模拟「迁移前老 schema」 — 显式插 NULL 字段
        # 实际生产里 schema.sql ALTER 会用 NOT NULL DEFAULT FALSE 兜底,本测试
        # 焦点是 _row_to_message 读侧对 None 的容错。
        await conn.execute(
            "INSERT INTO messages (channel_id, telegram_msg_id, text, date, "
            "is_favorite, tags, notes) "
            "VALUES ($1, $2, $3, $4, NULL, NULL, NULL) "
            "ON CONFLICT (channel_id, telegram_msg_id) DO NOTHING",
            999,
            1,
            "old row",
            datetime(2026, 9, 1, tzinfo=UTC),
        )
    m = await pg_repo.get_message(999, 1)
    assert m is not None
    assert m.is_favorite is False
    assert m.tags == []
    assert m.notes == ""
