"""存储后端 parity(2026-08-25 PR #3)— list_media + count_media_by_object_key 行为对齐。

2026-10-12 plan-batch3 目标 3 step 3.1:JSONL 后端已删,本文件只覆盖 in-mem
后端(`InMemoryRepository`);Postgres / Mongo 无本地集成测试桩(需 testcontainers
PG / mongomock_motor)— 走它们各自 backend unit test,`tests/integration/`
下 `test_pg_repo_*` / `test_mongo_repo_*` 跑真服务。Parity 测试矩阵的
PG / Mongo 实现挪到 step 3.4 `test_storage_backends_parity_mongo.py` 走 mongomock。

2026-09-15 PR 1c 旧版用 `seeded_repo` / `meta_seeded_repo` parametrize fixture
合并 in-mem / Jsonl twin-pair 测试;Jsonl 后端走真实文件 IO(验证 roundtrip +
落盘)。删 Jsonl 后,本文件保留 in-mem 测试替身;Postgres / Mongo parity 走
`tests/integration/test_*_repo_parity.py`(testcontainers PG / mongomock_motor)。
"""

from __future__ import annotations

from datetime import datetime

import pytest_asyncio

from tests.conftest import InMemoryRepository, make_message
from tgmonitor.core.dto import (
    ChannelDTO,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    ReactionDTO,
    SortDir,
    SortKey,
)
from tgmonitor.core.storage.repository import StorageRepository

# ---- 共享 helpers:种子数据 + media 工厂 ----


def _photo(
    idx: int = 0,
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
        object_key=object_key,
        object_backend="local",
        download_status=status,
    )


def _video(
    idx: int = 0,
    status: MediaDownloadStatus = MediaDownloadStatus.DONE,
    object_key: str | None = "media/clip_a.mp4",
    file_name: str = "clip_a.mp4",
) -> MediaDTO:
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


async def _seed_main_messages(repo: StorageRepository) -> None:
    """种主 fixture 的 4 条消息:跨 ch100/200/300,覆盖 DONE/FAILED/PENDING。"""
    await repo.save_message(
        make_message(
            channel_id=100,
            msg_id=1,
            date=datetime(2026, 1, 1, 10),
            media=[_photo(0, MediaDownloadStatus.DONE, "media/photo_a.jpg", "photo_a.jpg")],
        )
    )
    await repo.save_message(
        make_message(
            channel_id=100,
            msg_id=2,
            date=datetime(2026, 1, 2, 10),
            media=[
                _video(0, MediaDownloadStatus.DONE, "media/clip_a.mp4", "clip_a.mp4"),
                _photo(1, MediaDownloadStatus.FAILED, None, "failed.jpg"),
            ],
        )
    )
    await repo.save_message(
        make_message(
            channel_id=200,
            msg_id=10,
            date=datetime(2026, 1, 3, 10),
            media=[_photo(0, MediaDownloadStatus.DONE, "media/photo_a.jpg", "photo_a.jpg")],
        )
    )
    await repo.save_message(
        make_message(
            channel_id=300,
            msg_id=5,
            date=datetime(2026, 1, 4, 10),
            media=[_video(0, MediaDownloadStatus.PENDING, None, "pending.mp4")],
        )
    )


def _meta_msg(
    *,
    channel_id: int,
    msg_id: int,
    is_favorite: bool = False,
    tags: list[str] | None = None,
    is_pinned: bool = False,
    text: str = "x",
    date: datetime | None = None,
) -> MessageDTO:
    """构造带 metadata 的 MessageDTO(extend factories)。"""
    return MessageDTO(
        id=0,
        channel_id=channel_id,
        telegram_msg_id=msg_id,
        text=text,
        author="alice",
        date=date or datetime(2026, 1, 1, 12, 0, 0),
        is_favorite=is_favorite,
        tags=tags or [],
        is_pinned=is_pinned,
    )


async def _seed_meta_messages(repo: StorageRepository) -> None:
    """种 4 条覆盖 3 个 metadata 维度:1 全空,1 fav,1 tagged,1 pinned。"""
    repo_needs_subscribe = not isinstance(repo, InMemoryRepository)
    if repo_needs_subscribe:
        for cid in (1, 2):
            await repo.upsert_channel(ChannelDTO(id=cid, title=f"#{cid}"))
            await repo.set_channel_subscribed(cid, True)

    await repo.save_message(
        _meta_msg(channel_id=1, msg_id=100)  # 全空
    )
    await repo.save_message(_meta_msg(channel_id=1, msg_id=101, is_favorite=True))
    await repo.save_message(_meta_msg(channel_id=1, msg_id=102, tags=["tech", "ai"]))
    await repo.save_message(_meta_msg(channel_id=2, msg_id=200, is_pinned=True))


# ---- parametrize fixtures ----
# 设计:每个 parametrize 后端走一个真实实现(PR #A6 repo_backend 同款),
# 主 fixture `seeded_repo` 用主种子数据,`meta_seeded_repo` 用 PR #8 metadata
# 维度种子。
# 2026-10-12 plan-batch3 目标 3 step 3.1:JSONL 后端已删,parametrize
# 简化为单 `inmemory` 元素;Postgres / Mongo parity 测试挪到 step 3.4
# `test_storage_backends_parity_mongo.py` 走 mongomock_motor。
# 本地定义 — 范围清晰,只在 test_storage_backends.py 内部用,不放
# tests/fixtures/_storage.py 与其它后端测试共享(seed 是这个文件的 spec)。


@pytest_asyncio.fixture
async def seeded_repo() -> InMemoryRepository:
    """2026-10-12 plan-batch3 目标 3 step 3.1:仅 in-mem(seeded) — JSONL 已删。

    4 条 media seed:跨 ch100/200/300,覆盖 DONE/FAILED/PENDING。
    """
    repo = InMemoryRepository()
    await _seed_main_messages(repo)
    return repo


@pytest_asyncio.fixture
async def meta_seeded_repo() -> InMemoryRepository:
    """2026-10-12 plan-batch3 目标 3 step 3.1:仅 in-mem(metadata seeded)。

    频道 1/2(区别于主 fixture 的 100/200/300)— 4 条覆盖 fav/tagged/pinned。
    """
    repo = InMemoryRepository()
    await _seed_meta_messages(repo)
    return repo


# ---- 单后端 fixture(给 backend-specific 测试用) ----
# 2026-10-12 plan-batch3 目标 3 step 3.1:JSONL 已删,只剩 `in_mem_repo`
# 单一 fixture(in-mem 专属:直接 mutate `channels[100]` dict)。
# Jsonl 专属测试(close+restart roundtrip / 私有 `_root` 访问)随 step 3.1
# 同步删除(无对应 backend 替代,语义已迁 PG / Mongo 的 integration test)。


@pytest_asyncio.fixture
async def in_mem_repo() -> InMemoryRepository:
    """InMemory-only fixture — 给 backend-specific 测试(直接 mutate channels dict)。

    2026-10-12 plan-batch3 目标 3 step 3.1:Jsonl 已删,只剩此 in-mem 替身。
    """
    repo = InMemoryRepository()
    await _seed_main_messages(repo)
    return repo


# ---- list_media parity ----


async def test_list_media_no_filter_returns_all(seeded_repo):
    rows = await seeded_repo.list_media()
    # 4 条:msg1-photo / msg2-video / msg2-photo-failed / msg10-photo / msg5-video-pending
    assert len(rows) == 5


async def test_list_media_filter_by_status_done(seeded_repo):
    rows = await seeded_repo.list_media(status=MediaDownloadStatus.DONE)
    # msg1 photo DONE / msg2-video DONE / msg10 photo DONE = 3
    assert len(rows) == 3
    assert all(r[2].download_status == MediaDownloadStatus.DONE for r in rows)


async def test_list_media_filter_by_channel(seeded_repo):
    rows = await seeded_repo.list_media(channel_ids=[100])
    # ch=100 的 3 条 media(msg1 + msg2×2)
    assert len(rows) == 3
    assert all(r[0].channel_id == 100 for r in rows)


async def test_list_media_filter_by_type(seeded_repo):
    rows = await seeded_repo.list_media(media_type=MediaType.PHOTO)
    # 3 photo:msg1 / msg2-failed / msg10
    assert len(rows) == 3
    assert all(r[2].type == MediaType.PHOTO for r in rows)


async def test_list_media_search_case_insensitive(seeded_repo):
    # "PHOTO" 大写应命中 photo_a.jpg(大小写无关)
    rows = await seeded_repo.list_media(search="PHOTO")
    assert len(rows) == 2  # photo_a.jpg 出现两次(msg1 / msg10 共用 file_name)


async def test_list_media_limit_offset(seeded_repo):
    rows = await seeded_repo.list_media(limit=2)
    assert len(rows) == 2
    rows2 = await seeded_repo.list_media(limit=2, offset=2)
    assert len(rows2) == 2
    # offset 跳过前 2,后续不重叠
    keys_a = {(r[0].channel_id, r[0].telegram_msg_id, r[1]) for r in rows}
    keys_b = {(r[0].channel_id, r[0].telegram_msg_id, r[1]) for r in rows2}
    assert not (keys_a & keys_b)


# ---- 2026-09-28 修 Media Manager "All channels" 卡死 ----


async def test_list_media_skips_text_only_messages(seeded_repo):
    """2026-09-28:_filter_media_rows 跳过 `media=[]` 的行(无 media 的纯文本消息)
    — 既锁住正确性(这些行不应出现在结果),又防止后续误删优化导致全量 DTO
    构造、把"All channels" 框点回卡死状态。

    seeded_repo 默认 4 条带 media 的消息;额外种 2 条纯文本(无 media),
    验证 list_media 总数仍是 4,count_media 数字对齐。
    """
    # 拿一条既有消息当种子模板,造两条无 media 的"邻居"
    baseline = await seeded_repo.list_media()
    n_media = len(baseline)

    # 种 2 条纯文本到不同频道(2026-10-12 plan-batch3 目标 3 step 3.1:
    # JSONL 后端已删,InMemoryRepository 不需要「频道先注册」步骤,
    # 此处仅存消息即可)。

    text_only_a = make_message(channel_id=100, msg_id=900, media=[])
    text_only_b = make_message(channel_id=200, msg_id=901, media=[])
    await seeded_repo.save_message(text_only_a)
    await seeded_repo.save_message(text_only_b)

    # list_media:数量应仍 = n_media(纯文本行被跳)
    rows = await seeded_repo.list_media()
    assert len(rows) == n_media
    # 不应包含 text_only_a / text_only_b 的 telegram_msg_id
    msg_ids = {r[0].telegram_msg_id for r in rows}
    assert 900 not in msg_ids
    assert 901 not in msg_ids

    # count_media 走同一 helper,数字应一致
    assert await seeded_repo.count_media() == n_media


async def test_count_media_by_object_key_skips_text_only_messages(seeded_repo):
    """2026-09-28:`count_media_by_object_key` 同样跳过无 media 的行 — 防
    同样卡顿;且无 media 的 message 不可能贡献 refcount,数字保持正确。
    """
    baseline = await seeded_repo.count_media_by_object_key("media/photo_a.jpg")
    assert baseline == 2  # photo_a.jpg 在 msg1 + msg10

    # 加 5 条纯文本消息 — 不应影响 refcount
    # 2026-10-12 plan-batch3 目标 3 step 3.1:JSONL 后端已删,InMemory
    # 不需要「频道先注册」步骤,直接 save_message 即可。
    for cid, mid in ((100, 910), (100, 911), (200, 912), (300, 913), (300, 914)):
        await seeded_repo.save_message(make_message(channel_id=cid, msg_id=mid, media=[]))

    assert await seeded_repo.count_media_by_object_key("media/photo_a.jpg") == 2


# ---- count_media_by_object_key parity ----


async def test_count_refcount_shared_key(seeded_repo):
    # photo_a.jpg 在 msg1 + msg10 两次出现 → refcount=2
    assert await seeded_repo.count_media_by_object_key("media/photo_a.jpg") == 2


async def test_count_refcount_unique_key(seeded_repo):
    # clip_a.mp4 只在 msg2 出现 → refcount=1
    assert await seeded_repo.count_media_by_object_key("media/clip_a.mp4") == 1


async def test_count_refcount_missing_key(seeded_repo):
    assert await seeded_repo.count_media_by_object_key("media/never.jpg") == 0


async def test_count_refcount_empty_key(seeded_repo):
    # 空字符串:没 media 用 object_key="" — 返 0
    assert await seeded_repo.count_media_by_object_key("") == 0


async def test_count_refcount_counts_unsubscribed_channel_media(seeded_repo):
    """v1.8.0 patch:refcount 必须数全部 channel,与订阅状态无关(对齐 PG/Mongo)。

    旧实现 `list_subscribed_channels()` 过滤会让退订频道的 media 被
    漏数 → 删除同 key 的另一处 media 时误判 refcount=0 → 误删 bytes
    → 退订频道 message.media 仍引用该 key 的 dangling ref,重新订阅看到
    空媒体。fix 后 `count_media_by_object_key` 走 `self._channels.values()`
    全扫,与 Postgres `SELECT count(*) FROM media WHERE object_key=$1` /
    Mongo `$unwind + $match` 等价。
    """
    # baseline:订阅状态下 photo_a.jpg refcount=2(msg1 + msg10,见 seeded_repo)
    assert await seeded_repo.count_media_by_object_key("media/photo_a.jpg") == 2
    # 退订其中任一频道(msg1/channel 100 + msg10/channel 300 任一),旧 Jsonl
    # 实现会把 refcount 降到 1(漏数退订频道的引用),与 PG/Mongo 行为分叉。
    # 2026-10-12 plan-batch3 目标 3 step 3.1:Jsonl 已删,此断言仍守 PG/Mongo
    # 行为对齐 — 集成测试 `tests/integration/test_pg_repo_parity.py` / `test_mongo_repo_parity.py` 跑。
    if hasattr(seeded_repo, "set_channel_subscribed"):
        await seeded_repo.set_channel_subscribed(100, False)
        try:
            assert await seeded_repo.count_media_by_object_key("media/photo_a.jpg") == 2, (
                "refcount 必须数退订频道的 media(InMemory 行为,PG/Mongo 在集成测试守门)"
            )
        finally:
            await seeded_repo.set_channel_subscribed(100, True)


async def test_list_media_includes_unsubscribed_channel_media(seeded_repo):
    """v1.8.0 patch:`list_media(channel_ids=None)` 必须返回退订频道的 media。

    旧实现 `list_subscribed_channels()` 兜底让退订频道的 media 在
    Media Manager 里"消失",与 PG/Mongo 行为不一致。fix 后
    `_filter_media_rows` 走 `self._channels.values()` 全扫。
    """
    baseline = await seeded_repo.list_media()
    assert len(baseline) == 5  # 4 backend 共用 fixture(参 test_list_media_no_filter_returns_all)
    if hasattr(seeded_repo, "set_channel_subscribed"):
        # 退订 channel 100(msg1 + msg5 都有 media)
        await seeded_repo.set_channel_subscribed(100, False)
        try:
            rows = await seeded_repo.list_media()
            # 5 条都还在 — 退订不该让 media 从 Media Manager 消失
            assert len(rows) == 5
        finally:
            await seeded_repo.set_channel_subscribed(100, True)


# ---- 2026-08-25 v1.3.0 PR #6:排序 + 分页 + count_media parity ----------


async def test_list_media_sort_by_size_desc(seeded_repo):
    """PR #6:SortKey.SIZE + SortDir.DESC — 大文件优先。"""
    rows = await seeded_repo.list_media(
        sort=SortKey.SIZE,
        sort_dir=SortDir.DESC,
    )
    sizes = [r[2].file_size for r in rows]
    assert sizes == sorted(sizes, reverse=True)
    # 第一个是 video(5MB),后面是 photo(1KB)
    assert rows[0][2].type == MediaType.VIDEO


async def test_list_media_sort_by_status_asc(seeded_repo):
    """PR #6:SortKey.STATUS + SortDir.ASC — 字典序排序(do<fa<pe<do w/loading)。"""
    rows = await seeded_repo.list_media(
        sort=SortKey.STATUS,
        sort_dir=SortDir.ASC,
    )
    statuses = [r[2].download_status.value for r in rows]
    # 字典序 ASC:done<failed<pending
    assert statuses == sorted(statuses)
    assert rows[0][2].download_status == MediaDownloadStatus.DONE


async def test_list_media_sort_by_date_desc_default(seeded_repo):
    """PR #6:默认 sort=DATE / sort_dir=DESC(与 v1.2.0 既有行为对齐)。"""
    rows_default = await seeded_repo.list_media()
    rows_explicit = await seeded_repo.list_media(
        sort=SortKey.DATE,
        sort_dir=SortDir.DESC,
    )
    assert [r[2].object_key for r in rows_default] == [r[2].object_key for r in rows_explicit]


async def test_count_media_no_filter(seeded_repo):
    """PR #6:无 filter 数全部 media — fixture 含 5 条。"""
    # msg1=1 photo + msg2=2(video + photo) + msg10=1 photo + msg5=1 video = 5
    n = await seeded_repo.count_media()
    assert n == 5


async def test_count_media_with_filter(seeded_repo):
    """PR #6:count_media 应用 filter — status=DONE 只数下载完的。"""
    n = await seeded_repo.count_media(status=MediaDownloadStatus.DONE)
    # DONE: photo_a(msg1) + clip_a(msg2) + photo_a(msg10) = 3
    assert n == 3


async def test_count_media_pagination_consistency(seeded_repo):
    """PR #6:sum(分页的 len) == count_media 总数。"""
    total = await seeded_repo.count_media()
    seen = 0
    offset = 0
    limit = 2
    while True:
        rows = await seeded_repo.list_media(limit=limit, offset=offset)
        if not rows:
            break
        seen += len(rows)
        offset += limit
    assert seen == total


# ---- 2026-08-25 v1.3.0 PR #8:count_media_by_channel parity --------------


async def test_count_media_by_channel_matches_fixture(seeded_repo):
    """PR #8:count_media_by_channel = fixture 内 media 总数。

    fixture: ch100 msg1=1 photo + msg2=2 media + ch200 msg10=1 photo +
    ch300 msg5=1 video = 5
    """
    assert await seeded_repo.count_media_by_channel(100) == 3  # 1 + 2
    assert await seeded_repo.count_media_by_channel(200) == 1
    assert await seeded_repo.count_media_by_channel(300) == 1
    assert await seeded_repo.count_media_by_channel(999) == 0  # 空 channel


# ---------------------------------------------------------------------------
# 2026-08-27 v1.4.0 PR #12:list_messages 加 offset(导出真分页)— parity 测试
# ---------------------------------------------------------------------------


async def test_list_messages_limit_offset_tail_pagination(seeded_repo):
    """PR #12:offset 从尾部跳过 N 条再取 limit。

    fixture 4 条消息按 date asc 是 [msg1@1/1, msg2@1/2, msg10@1/3, msg5@1/4]。
    limit=2 offset=0 → 尾 2 条(升序)= [msg10, msg5]
    limit=2 offset=2 → 再往前 2 条(升序)= [msg1, msg2]
    limit=2 offset=4 → 空(数据耗尽)
    """
    page0 = await seeded_repo.list_messages(
        channel_ids=[100, 200, 300],
        limit=2,
        offset=0,
    )
    assert [m.telegram_msg_id for m in page0] == [10, 5]
    page1 = await seeded_repo.list_messages(
        channel_ids=[100, 200, 300],
        limit=2,
        offset=2,
    )
    assert [m.telegram_msg_id for m in page1] == [1, 2]
    page2 = await seeded_repo.list_messages(
        channel_ids=[100, 200, 300],
        limit=2,
        offset=4,
    )
    assert page2 == []


async def test_list_messages_offset_zero_unchanged(seeded_repo):
    """PR #12:offset=0 必须与 v1.3.0 行为完全一致(向后兼容)。

    v1.3.0 list_messages 走 `out[-limit:]` —— fixture 4 条尾 2 条升序 =
    [msg10, msg5]。这条是基线回归保护。
    """
    rows = await seeded_repo.list_messages(
        channel_ids=[100, 200, 300],
        limit=2,
        offset=0,
    )
    assert [m.telegram_msg_id for m in rows] == [10, 5]


# ---------------------------------------------------------------------------
# 2026-08-27 v1.4.0 PR #14:`update_channel_metadata` 部分更新 parity。
# - 只动非 None 字段,其余保留
# - is_subscribed 不动(本方法是「真元数据」更新)
# - 不存在 channel idempotent 不抛
# ---------------------------------------------------------------------------


async def test_update_channel_metadata_nonexistent_silent(seeded_repo):
    """PR #14:不存在的 channel 调 update_channel_metadata 不抛(幂等)。"""
    # 999 不在 fixture 里
    await seeded_repo.update_channel_metadata(999, title="x", member_count=1)
    # 仍然不存在
    assert await seeded_repo.get_channel(999) is None


# ---------------------------------------------------------------------------
# 2026-08-27 v1.4.0 PR #15:`aggregate_per_channel` Dashboard N+1 → 单查询 parity。
# - 4 字段聚合:messages / media / done_media / last_date
# - 缺失 channel_id 不在返 dict 里
# - 空 channel_ids 返 {}
# ---------------------------------------------------------------------------


async def test_aggregate_per_channel(seeded_repo):
    """PR #15:一次聚合 4 字段。

    fixture 已有 ch100 (msg1=1 photo done, msg2=2 media [video done, photo failed])
    + ch200 (msg10=1 photo done) + ch300 (msg5=1 video pending)。
    """
    bucket = await seeded_repo.aggregate_per_channel([100, 200, 300])
    # ch100:2 msgs, 3 media, 2 done (video+photo)
    assert bucket[100].messages == 2
    assert bucket[100].media == 3
    assert bucket[100].done_media == 2
    assert bucket[100].last_date == datetime(2026, 1, 2, 10)
    # ch200:1 msg, 1 media, 1 done
    assert bucket[200].messages == 1
    assert bucket[200].media == 1
    assert bucket[200].done_media == 1
    # ch300:1 msg, 1 media, 0 done (pending)
    assert bucket[300].messages == 1
    assert bucket[300].media == 1
    assert bucket[300].done_media == 0


async def test_aggregate_missing_channel_omitted(seeded_repo):
    """PR #15:缺失 channel(无消息)不在返 dict 里。"""
    bucket = await seeded_repo.aggregate_per_channel([999])
    assert bucket == {}


async def test_aggregate_empty_input(seeded_repo):
    """PR #15:channel_ids=[] → 返 {}。"""
    bucket = await seeded_repo.aggregate_per_channel([])
    assert bucket == {}


# ---- v1.6.0 PR #Q2:photo_local_key 部分更新 parity(InMemory) -----
# 2026-10-12 plan-batch3 目标 3 step 3.1:Jsonl 已删,InMemory parity 测试保留;
# Postgres / Mongo 走 `tests/integration/test_*_repo_parity.py`。


async def test_update_channel_metadata_preserves_other_fields_on_photo_update(seeded_repo):
    """PR #Q2:`photo_local_key` 单独更新不动 title / username / member_count。"""
    await seeded_repo.upsert_channel(
        ChannelDTO(id=100, title="Title", username="@u", member_count=99)
    )
    await seeded_repo.update_channel_metadata(100, photo_local_key="/tmp/x.png")
    got = await seeded_repo.get_channel(100)
    assert got is not None
    assert got.photo_local_key == "/tmp/x.png"
    assert got.title == "Title"
    assert got.username == "@u"
    assert got.member_count == 99


# ---- 2026-08-27 v1.4.0 PR #10:`update_message_interactions` 增量更新(views /
# reactions)—— InMemory parity。reactions 可以是 ReactionDTO 或 dict。
# 2026-10-12 plan-batch3 目标 3 step 3.1:Jsonl 已删,InMemory 替身验证
# reactions 序列化层转换;PG / Mongo 走各自 backend unit test。
# ----


async def test_update_views_only(seeded_repo):
    """PR #10:只传 views → views 更新,reactions 不动。"""
    import dataclasses

    base = make_message(channel_id=100, msg_id=5500)
    msg = dataclasses.replace(base, views=10)
    await seeded_repo.save_message(msg)
    await seeded_repo.update_message_interactions(100, 5500, views=99)
    got = next(
        r for r in (await seeded_repo.list_messages(channel_ids=[100])) if r.telegram_msg_id == 5500
    )
    assert got.views == 99
    # reactions 字段未初始化(原 save_message 时没设)→ 仍 None
    assert got.reactions is None


async def test_update_reactions_only(seeded_repo):
    """PR #10:只传 reactions → reactions 更新,views 不动。"""
    import dataclasses

    base = make_message(channel_id=100, msg_id=5501)
    msg = dataclasses.replace(base, views=42)
    await seeded_repo.save_message(msg)
    new_rxns = [
        ReactionDTO(type="emoji", emoji="🎉", count=5, is_chosen=True),
        ReactionDTO(type="emoji", emoji="👍", count=3, is_chosen=False),
    ]
    await seeded_repo.update_message_interactions(100, 5501, reactions=new_rxns)
    got = next(
        r for r in (await seeded_repo.list_messages(channel_ids=[100])) if r.telegram_msg_id == 5501
    )
    assert got.views == 42  # 未动
    assert got.reactions is not None
    assert len(got.reactions) == 2
    assert got.reactions[0].emoji == "🎉"
    assert got.reactions[0].is_chosen is True
    assert got.reactions[1].count == 3


async def test_update_reactions_empty_list_clears(seeded_repo):
    """PR #10:`reactions=[]` 显式清空(不是不动)。"""
    import dataclasses

    base = make_message(channel_id=100, msg_id=5502)
    msg = dataclasses.replace(
        base,
        reactions=[ReactionDTO(type="emoji", emoji="😢", count=1, is_chosen=False)],
    )
    await seeded_repo.save_message(msg)
    # reactions=None 不动 → 仍有反应;reactions=[] 清空
    await seeded_repo.update_message_interactions(100, 5502, reactions=[])
    got = next(
        r for r in (await seeded_repo.list_messages(channel_ids=[100])) if r.telegram_msg_id == 5502
    )
    assert got.reactions == []


async def test_update_nonexistent_message_silent(seeded_repo):
    """PR #10:消息不存在 idempotent 不抛(TDLib 偶尔推陈年 view 更新)。"""
    # 不 save,直接 update
    await seeded_repo.update_message_interactions(999, 9999, views=100)
    # 落库后仍查不到(没保存这条消息)
    rows = await seeded_repo.list_messages(channel_ids=[999])
    assert rows == []


# ============================================================
# 2026-09-10 v1.7.3:`update_message_pin` 4 后端 parity 测试
# ============================================================


async def test_update_pin_nonexistent_silent(seeded_repo):
    """v1.7.3:update_message_pin 对不存在消息 idempotent 不抛(与 reactions 一致)。"""
    # 不存任何 message,直接 update → no-op
    await seeded_repo.update_message_pin(999, 1, True)
    # 仍 None
    assert await seeded_repo.get_message(999, 1) is None


# ============================================================
# 2026-09-14 v1.7.5 PR #8:`list_messages` favorite_only / tag_only / pinned_only
# 3 个 kwarg 在 InMemory 替身上的行为验证。
# 2026-10-12 plan-batch3 目标 3 step 3.1:Jsonl 已删;PG / Mongo parity 走
# `tests/integration/test_*_repo_parity.py` 集成测试。
# ============================================================


async def test_pr8_list_messages_favorite_only(meta_seeded_repo):
    """PR #8:`list_messages(favorite_only=True)` 只返 is_favorite=True。"""
    msgs = await meta_seeded_repo.list_messages(channel_ids=[1, 2], favorite_only=True)
    ids = sorted(m.telegram_msg_id for m in msgs)
    assert ids == [101]


async def test_pr8_list_messages_tag_only(meta_seeded_repo):
    """PR #8:`list_messages(tag_only=True)` 只返 tags 非空。"""
    msgs = await meta_seeded_repo.list_messages(channel_ids=[1, 2], tag_only=True)
    ids = sorted(m.telegram_msg_id for m in msgs)
    assert ids == [102]


async def test_pr8_list_messages_pinned_only(meta_seeded_repo):
    """PR #8:`list_messages(pinned_only=True)` 只返 is_pinned=True。"""
    msgs = await meta_seeded_repo.list_messages(channel_ids=[1, 2], pinned_only=True)
    ids = sorted(m.telegram_msg_id for m in msgs)
    assert ids == [200]


async def test_pr8_list_messages_combined_and(meta_seeded_repo):
    """PR #8:3 filter 同时开 → AND 语义(任何 1 条都只命中 1 个条件)。"""
    msgs = await meta_seeded_repo.list_messages(
        channel_ids=[1, 2],
        favorite_only=True,
        tag_only=True,
        pinned_only=True,
    )
    # 没有 1 条消息同时是 fav + tagged + pinned → 0 results
    assert msgs == []


async def test_pr8_list_messages_combined_with_text(meta_seeded_repo):
    """PR #8:text + favorite + pinned 组合 → AND 语义生效。

    种 1 条 `fav + pinned` + text 含 "needle" 验证同时 3 个 filter 命中。
    """
    await meta_seeded_repo.save_message(
        _meta_msg(
            channel_id=1,
            msg_id=300,
            is_favorite=True,
            is_pinned=True,
            text="needle in haystack",
        )
    )
    msgs = await meta_seeded_repo.list_messages(
        channel_ids=[1, 2],
        search="needle",
        favorite_only=True,
        pinned_only=True,
    )
    ids = sorted(m.telegram_msg_id for m in msgs)
    assert ids == [300]


async def test_pr8_list_messages_no_filter_returns_all(meta_seeded_repo):
    """PR #8 regression:不加 metadata filter 时,所有 4 条都返(向后兼容)。"""
    msgs = await meta_seeded_repo.list_messages(channel_ids=[1, 2])
    assert len(msgs) == 4


# ============================================================
# Backend-specific 测试(2026-10-12 plan-batch3 目标 3 step 3.1:Jsonl 已删)
# 仅 in-mem 替身的「直接 mutate `channels[id]`」测试;落盘 roundtrip /
# dict 格式兼容这些 Jsonl 专属行为已迁 `tests/integration/test_*_repo_parity.py`。
# ============================================================


# ---- PR #14 部分更新:InMemory 走直接 mutate `channels[id]` 验证语义 ---


async def test_in_mem_update_channel_metadata_partial(in_mem_repo):
    """PR #14:InMemory 部分更新 — 只传 title → 其它字段保留。"""
    # fixture 已隐式建频道 100/200/300;补一个显式订阅 + 初始 username
    in_mem_repo.channels[100] = ChannelDTO(
        id=100,
        title="Old Title",
        username="oldname",
        member_count=50,
    )
    await in_mem_repo.set_channel_subscribed(100, True)
    # 用 update_channel_metadata 只改 title + member_count
    await in_mem_repo.update_channel_metadata(
        100,
        title="New Title",
        member_count=500,
    )
    got = await in_mem_repo.get_channel(100)
    assert got is not None
    assert got.title == "New Title"  # 改了
    assert got.username == "oldname"  # 保留
    assert got.member_count == 500  # 改了


async def test_in_mem_update_channel_metadata_preserves_subscribed(in_mem_repo):
    """PR #14:InMemory 部分更新不动 is_subscribed —— 订阅标志由
    set_channel_subscribed 单独维护。"""
    in_mem_repo.channels[100] = ChannelDTO(
        id=100,
        title="t",
        username="u",
        member_count=10,
    )
    await in_mem_repo.set_channel_subscribed(100, True)
    # 即使 title/username 都重写,is_subscribed 不该变
    await in_mem_repo.update_channel_metadata(
        100,
        title="New",
        username="new",
        member_count=999,
    )
    got = await in_mem_repo.get_channel(100)
    assert got is not None
    assert got.is_subscribed is True
    assert got.title == "New"
    assert got.username == "new"
    assert got.member_count == 999


# ---- PR #14 「只传一个字段」测试 — InMemory 走单字段更新 —


async def test_in_mem_update_channel_metadata_only_title(in_mem_repo):
    """PR #14:None 参数保留旧值;只传 title 时 username/member_count 不动。"""
    in_mem_repo.channels[100] = ChannelDTO(
        id=100,
        title="old",
        username="u",
        member_count=42,
    )
    await in_mem_repo.set_channel_subscribed(100, True)
    await in_mem_repo.update_channel_metadata(100, title="only-title")
    got = await in_mem_repo.get_channel(100)
    assert got.title == "only-title"
    assert got.username == "u"
    assert got.member_count == 42


# ---- PR #Q2 photo_local_key — InMemory 直接 mutate,无 roundtrip ---


async def test_in_mem_update_channel_metadata_photo_local_key(in_mem_repo):
    """PR #Q2:`update_channel_metadata(photo_local_key=...)` 部分更新。"""
    await in_mem_repo.upsert_channel(ChannelDTO(id=100, title="Old", member_count=10))
    await in_mem_repo.update_channel_metadata(100, photo_local_key="/tmp/avatar.jpg")
    got = await in_mem_repo.get_channel(100)
    assert got is not None
    assert got.photo_local_key == "/tmp/avatar.jpg"
    assert got.title == "Old"  # 未动
    assert got.member_count == 10  # 未动


# ---- v1.6.4:4 个 spammer 字段 — InMemory 直接读回验证语义 ---


async def test_in_mem_update_channel_metadata_4_tdlib_fields(in_mem_repo):
    """v1.6.4:InMemory 4 字段部分更新 + 其它字段保留。"""
    await in_mem_repo.upsert_channel(
        ChannelDTO(
            id=100,
            title="Old",
            username="@u",
            member_count=10,
            is_verified=True,
        )
    )
    # 一次更新 4 个 spammer 字段
    await in_mem_repo.update_channel_metadata(
        100,
        is_verified=False,
        is_scam=True,
        is_fake=True,
        has_protected_content=True,
    )
    got = await in_mem_repo.get_channel(100)
    assert got is not None
    assert got.is_verified is False  # 显式 False 覆盖
    assert got.is_scam is True
    assert got.is_fake is True
    assert got.has_protected_content is True
    assert got.title == "Old"  # 保留
    assert got.username == "@u"  # 保留
    assert got.member_count == 10  # 保留


# ---- v1.6.4:4 字段部分更新 — InMemory 替身单字段更新 ---


async def test_in_mem_update_channel_metadata_4_fields_partial_keeps_others(in_mem_repo):
    """v1.6.4:只传 is_verified,其它 3 字段保持 None 默认。"""
    await in_mem_repo.upsert_channel(
        ChannelDTO(
            id=100,
            title="X",
            is_verified=True,
            is_scam=True,
            is_fake=False,
            has_protected_content=False,
        )
    )
    # 只动 is_verified
    await in_mem_repo.update_channel_metadata(100, is_verified=False)
    got = await in_mem_repo.get_channel(100)
    assert got is not None
    assert got.is_verified is False  # 改了
    assert got.is_scam is True  # 保留
    assert got.is_fake is False  # 保留
    assert got.has_protected_content is False  # 保留


# ---- v1.6.4:upsert_channel roundtrip — InMemory 直接读 ---


async def test_in_mem_upsert_channel_roundtrip_4_fields(in_mem_repo):
    """v1.6.4:`upsert_channel` 写 4 字段 → `get_channel` 读回一致。"""
    await in_mem_repo.upsert_channel(
        ChannelDTO(
            id=100,
            title="T",
            is_verified=True,
            is_scam=True,
            is_fake=True,
            has_protected_content=True,
        )
    )
    got = await in_mem_repo.get_channel(100)
    assert got is not None
    assert got.is_verified is True
    assert got.is_scam is True
    assert got.is_fake is True
    assert got.has_protected_content is True


# ---- PR #10 update_both_views_and_reactions — InMemory 替身 ---


async def test_in_mem_update_both_views_and_reactions(in_mem_repo):
    """PR #10:同时传 views 和 reactions 都更新。"""
    await in_mem_repo.save_message(make_message(channel_id=100, msg_id=5503))
    rxns = [ReactionDTO(type="emoji", emoji="🚀", count=10, is_chosen=False)]
    await in_mem_repo.update_message_interactions(
        100,
        5503,
        views=500,
        reactions=rxns,
    )
    got = next(
        r for r in (await in_mem_repo.list_messages(channel_ids=[100])) if r.telegram_msg_id == 5503
    )
    assert got.views == 500
    assert got.reactions is not None and got.reactions[0].count == 10


# ---------------------------------------------------------------------------
# 2026-08-27 v1.4.0 PR #9:MessageDTO 加 forward_origin / via_bot_user_id /
# media_album_id / is_pinned(reply_to_msg_id 已存在)— 后端 roundtrip parity。
# 2026-10-12 plan-batch3 目标 3 step 3.1:Jsonl 已删,只覆盖 InMemory;
# PG / Mongo 走 `tests/integration/test_*_repo_parity.py` 集成测试。
# ---------------------------------------------------------------------------


async def test_in_mem_roundtrip_pr9_extra_fields(in_mem_repo):
    """PR #9:5 个 TDLib Message 字段 InMemory roundtrip 不丢。

    InMemory 直接持 DTO 对象,字段在内存中天然保留 —— 但用真实构造 + save +
    list 全链路确保 mock 不到任何东西。msg_id 用 4242 避开 fixture 已有消息。
    """
    import dataclasses

    base = make_message(channel_id=100, msg_id=4242)
    msg = dataclasses.replace(
        base,
        reply_to_msg_id=10,
        forward_origin={
            "@type": "messageOriginUser",
            "sender_user_id": 999,
            "date": 1700000000,
        },
        via_bot_user_id=12345,
        media_album_id=8888,
        is_pinned=True,
    )
    await in_mem_repo.save_message(msg)
    rows = await in_mem_repo.list_messages(channel_ids=[100])
    got = next(r for r in rows if r.telegram_msg_id == 4242)
    assert got.reply_to_msg_id == 10
    assert got.forward_origin == {
        "@type": "messageOriginUser",
        "sender_user_id": 999,
        "date": 1700000000,
    }
    assert got.via_bot_user_id == 12345
    assert got.media_album_id == 8888
    assert got.is_pinned is True


# ---- PR #9:旧数据(无新字段)读出来 4 个新字段默认 None / False — 后端行为一致,可合并 ----


async def test_roundtrip_pr9_extra_fields_default_none(seeded_repo):
    """PR #9:旧数据(无新字段)读出来 4 个新字段都默认 None / False。

    backward-compat 测试 —— 旧消息(无新字段)读出来必须静默回退到默认值,
    不能 raise KeyError。
    """
    msg = make_message(channel_id=100, msg_id=4243)
    # make_message 用 base,没设新字段,默认值 = None / False
    await seeded_repo.save_message(msg)
    rows = await seeded_repo.list_messages(channel_ids=[100])
    got = next(r for r in rows if r.telegram_msg_id == 4243)
    assert got.reply_to_msg_id is None
    assert got.forward_origin is None
    assert got.via_bot_user_id is None
    assert got.media_album_id is None
    assert got.is_pinned is False


# ============================================================
# 2026-09-10 v1.7.3:`update_message_pin` — InMemory 替身直接 mutate
# ============================================================


async def test_in_mem_update_pin_sets_true(in_mem_repo):
    """v1.7.3:InMemory `update_message_pin(True)` → DTO.is_pinned 立刻变 True。"""
    await in_mem_repo.save_message(make_message(channel_id=100, msg_id=1))
    # 初始 is_pinned=False(make_message 默认)
    msg = await in_mem_repo.get_message(100, 1)
    assert msg is not None and msg.is_pinned is False
    # 设为 True
    await in_mem_repo.update_message_pin(100, 1, True)
    msg = await in_mem_repo.get_message(100, 1)
    assert msg is not None and msg.is_pinned is True
    # 改回 False
    await in_mem_repo.update_message_pin(100, 1, False)
    msg = await in_mem_repo.get_message(100, 1)
    assert msg is not None and msg.is_pinned is False
