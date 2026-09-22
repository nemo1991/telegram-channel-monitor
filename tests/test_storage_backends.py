"""4 存储后端 parity(2026-08-25 PR #3)— list_media + count_media_by_object_key 行为对齐。

只覆盖能跑的两后端(InMemory + JsonlFileStore)— Postgres / Mongo
无本地集成测试桩(`asyncpg` 需要 PG server,`mongomock_motor` 未装);
contract 一致性由它们各自的 backend unit test 保证(后续按需补)。

断言集中在 `test_*_parity_*` 系列 — 同一组 input,两后端产出必须一致
(顺序由 backend 自己定,顺序差异在 spec 里说明)。

2026-09-15 PR 1c:用 `seeded_repo` / `meta_seeded_repo` parametrize fixture
合并 in-mem / jsonl twin-pair 测试,消除重复。jsonl 后端仍走真实文件 IO
(验证 roundtrip + 落盘),in-mem 走 `InMemoryRepository` 测试替身(等语义)。
"""

from __future__ import annotations

from datetime import datetime
from typing import AsyncIterator

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
from tgmonitor.core.storage.jsonl_store import JsonlFileStore
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
# 维度种子。jsonl 后端在种子前先 upsert_channel + set_channel_subscribed
# 三个频道(否则 list_media 走 list_subscribed_channels 漏掉消息)。
# 本地定义 — 范围清晰,只在 test_storage_backends.py 内部用,不放
# tests/fixtures/_storage.py 与其它后端测试共享(seed 是这个文件的 spec)。


@pytest_asyncio.fixture(params=["inmemory", "jsonl"])
async def seeded_repo(request, tmp_path) -> AsyncIterator[StorageRepository]:
    """2026-09-15 PR 1c:inmemory + jsonl 两后端 parametrize + 4 条 media seed。

    jsonl 后端:`upsert_channel` + `set_channel_subscribed(True)` 必须先于
    `save_message` 调用(list_media 走 list_subscribed_channels 过滤)。
    """
    backend = request.param
    if backend == "inmemory":
        repo = InMemoryRepository()
        await _seed_main_messages(repo)
        yield repo
    elif backend == "jsonl":
        repo = JsonlFileStore(root=tmp_path / "jsonl")
        await repo.connect()
        # 频道需要「已订阅」才会被 jsonl 后端扫描到(list_media 走 list_subscribed_channels)
        for cid in (100, 200, 300):
            await repo.upsert_channel(ChannelDTO(id=cid, title=f"#{cid}"))
            await repo.set_channel_subscribed(cid, True)
        await _seed_main_messages(repo)
        try:
            yield repo
        finally:
            await repo.close()


@pytest_asyncio.fixture(params=["inmemory", "jsonl"])
async def meta_seeded_repo(request, tmp_path) -> AsyncIterator[StorageRepository]:
    """2026-09-15 PR 1c:PR #8 metadata 维度种子的 parametrize fixture。

    频道 1/2(区别于主 fixture 的 100/200/300)— 4 条覆盖 fav/tagged/pinned。
    """
    backend = request.param
    if backend == "inmemory":
        repo = InMemoryRepository()
        await _seed_meta_messages(repo)
        yield repo
    elif backend == "jsonl":
        repo = JsonlFileStore(root=tmp_path / "jsonl_meta")
        await repo.connect()
        await repo.init_schema()
        await _seed_meta_messages(repo)
        try:
            yield repo
        finally:
            await repo.close()


# ---- 单后端 fixture(给 backend-specific 测试用) ----
# 这些是「行为有差异」的测试:in-mem-only / jsonl-only / jsonl persistence
# roundtrip / 私有属性访问,无法用一个统一 parametrize 跑两个后端。
# `jsonl_repo` 给 jsonl 专属测试(含 close+restart roundtrip),保留
# `_root` 私有属性访问入口;`in_mem_repo` 给 in-mem 专属测试 + 私有
# `channels[100]` 直接 mutate。


@pytest_asyncio.fixture
async def in_mem_repo() -> InMemoryRepository:
    """InMemory-only fixture — 给 backend-specific 测试(直接 mutate channels dict)。"""
    repo = InMemoryRepository()
    await _seed_main_messages(repo)
    return repo


@pytest_asyncio.fixture
async def jsonl_repo(tmp_path) -> JsonlFileStore:
    """Jsonl-only fixture — 给 jsonl 专属测试 + 落盘 roundtrip(close+reopen)。"""
    repo = JsonlFileStore(root=tmp_path)
    await repo.connect()
    await repo.init_schema()
    for cid in (100, 200, 300):
        await repo.upsert_channel(ChannelDTO(id=cid, title=f"#{cid}"))
        await repo.set_channel_subscribed(cid, True)
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
    # 退订其中任一频道(msg1/channel 100 + msg10/channel 300 任一),Jsonl 旧实现
    # 会把 refcount 降到 1(漏数退订频道的引用),与 PG/Mongo 行为分叉。
    # 注:InMemory 不分订阅,所以不受影响 — 此断言主要盯 Jsonl。
    if hasattr(seeded_repo, "set_channel_subscribed"):
        await seeded_repo.set_channel_subscribed(100, False)
        try:
            assert await seeded_repo.count_media_by_object_key("media/photo_a.jpg") == 2, (
                "refcount 必须数退订频道的 media(Jsonl 与 PG/Mongo 对齐)"
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


# ---- v1.6.0 PR #Q2:photo_local_key 部分更新 parity(InMemory + Jsonl) -----


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
# reactions)—— InMemory + Jsonl parity。reactions 可以是 ReactionDTO 或 dict。
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
# 3 个 kwarg 在 InMemory + Jsonl 两后端的 parity 验证。
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
# Backend-specific 测试(行为有差异,无法统一 parametrize)
# - 不同 setup 方式(in-mem 直接 mutate `channels[id]` vs jsonl 用 `upsert_channel`)
# - jsonl 专属落盘 roundtrip(close + 重启验证持久化)
# - jsonl 专属 dict 格式兼容(reactions 元素是 dict 而非 ReactionDTO)
# - in-mem 专属(无 jsonl twin)
# ============================================================


# ---- PR #14 部分更新:setup 方式不同(in-mem 直接 mutate, jsonl 走 upsert) --


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


async def test_jsonl_update_channel_metadata_partial(jsonl_repo):
    """PR #14:Jsonl 部分更新 — 只传 member_count → 其它字段保留 + 落盘。"""
    # jsonl_repo fixture:ch100 已订阅 + title 默认「#100」,补一个 username
    existing = await jsonl_repo.get_channel(100)
    assert existing is not None
    await jsonl_repo.upsert_channel(
        ChannelDTO(
            id=100,
            title=existing.title,
            username="oldname",
            member_count=50,
            created_at=existing.created_at,
            is_subscribed=True,
            last_synced_at=existing.last_synced_at,
        )
    )
    # 只改 member_count
    await jsonl_repo.update_channel_metadata(100, member_count=999)
    got = await jsonl_repo.get_channel(100)
    assert got is not None
    assert got.member_count == 999  # 改了
    assert got.username == "oldname"  # 保留
    assert got.title == existing.title  # 保留


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


async def test_jsonl_update_channel_metadata_preserves_subscribed(jsonl_repo):
    """PR #14:Jsonl 同上 —— is_subscribed 由 set_channel_subscribed 单独维护。"""
    # jsonl_repo fixture 中 ch100 已 set_channel_subscribed(True)
    await jsonl_repo.update_channel_metadata(100, title="Renamed", member_count=10)
    got = await jsonl_repo.get_channel(100)
    assert got is not None
    assert got.is_subscribed is True  # 保留 True
    assert got.title == "Renamed"  # 改了


# ---- PR #14 「只传一个字段」测试 — in-mem / jsonl 测试不同字段,无 twin ----


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


async def test_jsonl_update_channel_metadata_only_member_count(jsonl_repo):
    """PR #14:Jsonl 只传 member_count,其它字段不动。"""
    await jsonl_repo.update_channel_metadata(100, member_count=7)
    got = await jsonl_repo.get_channel(100)
    # jsonl_repo fixture upsert 默认 title="#100",username/created_at 留 None
    assert got.member_count == 7
    assert got.title == "#100"
    assert got.username is None


# ---- PR #Q2 photo_local_key — in-mem 无 roundtrip, jsonl 走 close+reopen 验证落盘 ----


async def test_in_mem_update_channel_metadata_photo_local_key(in_mem_repo):
    """PR #Q2:`update_channel_metadata(photo_local_key=...)` 部分更新。"""
    await in_mem_repo.upsert_channel(ChannelDTO(id=100, title="Old", member_count=10))
    await in_mem_repo.update_channel_metadata(100, photo_local_key="/tmp/avatar.jpg")
    got = await in_mem_repo.get_channel(100)
    assert got is not None
    assert got.photo_local_key == "/tmp/avatar.jpg"
    assert got.title == "Old"  # 未动
    assert got.member_count == 10  # 未动


async def test_jsonl_update_channel_metadata_photo_local_key(jsonl_repo):
    """PR #Q2:Jsonl 同上 + 落盘 roundtrip。"""
    await jsonl_repo.upsert_channel(ChannelDTO(id=100, title="Old", member_count=10))
    await jsonl_repo.update_channel_metadata(100, photo_local_key="/tmp/avatar.jpg")
    got = await jsonl_repo.get_channel(100)
    assert got is not None
    assert got.photo_local_key == "/tmp/avatar.jpg"
    assert got.title == "Old"
    # 重启 — 重新 open 应看到 photo_local_key(测试 JsonlFileStore 持久化)
    await jsonl_repo.close()
    jsonl_repo2 = JsonlFileStore(root=jsonl_repo._root)  # type: ignore[attr-defined]
    await jsonl_repo2.connect()
    got2 = await jsonl_repo2.get_channel(100)
    assert got2 is not None
    assert got2.photo_local_key == "/tmp/avatar.jpg"
    await jsonl_repo2.close()


# ---- v1.6.4:4 个 spammer 字段 — jsonl 加 roundtrip 验证 JSON 序列化 ----


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


async def test_jsonl_update_channel_metadata_4_tdlib_fields(jsonl_repo):
    """v1.6.4:Jsonl 4 字段部分更新 + 落盘 roundtrip。"""
    await jsonl_repo.upsert_channel(
        ChannelDTO(
            id=100,
            title="Old",
            username="@u",
            member_count=10,
            is_verified=True,
            has_protected_content=False,
        )
    )
    await jsonl_repo.update_channel_metadata(
        100,
        is_scam=True,
        is_fake=True,
    )
    got = await jsonl_repo.get_channel(100)
    assert got is not None
    assert got.is_verified is True  # 没传,保留
    assert got.is_scam is True  # 改了
    assert got.is_fake is True  # 改了
    assert got.has_protected_content is False  # 没传,保留
    # 重启 roundtrip 验证 JSON 序列化没漏字段
    await jsonl_repo.close()
    jsonl_repo2 = JsonlFileStore(root=jsonl_repo._root)  # type: ignore[attr-defined]
    await jsonl_repo2.connect()
    got2 = await jsonl_repo2.get_channel(100)
    assert got2 is not None
    assert got2.is_verified is True
    assert got2.is_scam is True
    assert got2.is_fake is True
    assert got2.has_protected_content is False
    await jsonl_repo2.close()


# ---- v1.6.4:4 字段部分更新 — in-mem / jsonl 测试不同字段 + 不同值 ----


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


async def test_jsonl_update_channel_metadata_4_fields_partial_keeps_others(jsonl_repo):
    """v1.6.4:Jsonl 同上 — 只传 1 个,其它 3 字段保持。"""
    await jsonl_repo.upsert_channel(
        ChannelDTO(
            id=100,
            title="X",
            is_verified=True,
            is_scam=True,
            is_fake=True,
            has_protected_content=True,
        )
    )
    # 只动 has_protected_content
    await jsonl_repo.update_channel_metadata(100, has_protected_content=False)
    got = await jsonl_repo.get_channel(100)
    assert got is not None
    assert got.is_verified is True  # 保留
    assert got.is_scam is True  # 保留
    assert got.is_fake is True  # 保留
    assert got.has_protected_content is False  # 改了


# ---- v1.6.4:upsert_channel roundtrip — in-mem 直接读, jsonl 走 close+reopen ----


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


async def test_jsonl_upsert_channel_roundtrip_4_fields(jsonl_repo):
    """v1.6.4:Jsonl `upsert_channel` 写 4 字段 → 重启后 `get_channel` 读回。"""
    await jsonl_repo.upsert_channel(
        ChannelDTO(
            id=100,
            title="T",
            is_verified=True,
            is_scam=False,
            is_fake=True,
            has_protected_content=False,
        )
    )
    # roundtrip
    await jsonl_repo.close()
    jsonl_repo2 = JsonlFileStore(root=jsonl_repo._root)  # type: ignore[attr-defined]
    await jsonl_repo2.connect()
    got = await jsonl_repo2.get_channel(100)
    assert got is not None
    assert got.is_verified is True
    assert got.is_scam is False
    assert got.is_fake is True
    assert got.has_protected_content is False
    await jsonl_repo2.close()


# ---- PR #10 update_both_views_and_reactions — 仅 in-mem(无 jsonl twin) ----


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


# ---- PR #10 update_reactions_accepts_dict_format — 仅 jsonl(测试 jsonl 专属 dict → DTO 转换) ----


async def test_jsonl_update_reactions_accepts_dict_format(jsonl_repo):
    """PR #10:reactions 元素可以是 dict(老序列化格式兼容)→ 内部 to ReactionDTO。"""
    msg = make_message(channel_id=200, msg_id=2603)
    await jsonl_repo.save_message(msg)
    rxns_dict = [
        {"type": "emoji", "emoji": "😀", "count": 5, "is_chosen": False},
    ]
    await jsonl_repo.update_message_interactions(200, 2603, reactions=rxns_dict)
    got = next(
        r for r in (await jsonl_repo.list_messages(channel_ids=[200])) if r.telegram_msg_id == 2603
    )
    assert got.reactions is not None
    assert got.reactions[0].emoji == "😀"


# ---------------------------------------------------------------------------
# 2026-08-27 v1.4.0 PR #9:MessageDTO 加 forward_origin / via_bot_user_id /
# media_album_id / is_pinned(reply_to_msg_id 已存在)— 后端 roundtrip parity。
# 只覆盖能跑的 InMemory + Jsonl 两后端,PG/Mongo 走自己 backend unit test。
#
# 不同后端用不同 forward_origin / via_bot_user_id / 字段值(测不同输入场景),
# 不能合并成一个 parametrize,保留两个独立测试。
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


async def test_jsonl_roundtrip_pr9_extra_fields(jsonl_repo):
    """PR #9:Jsonl roundtrip 把 forward_origin / via_bot_user_id /
    media_album_id / is_pinned 写盘再读回必须一致。"""
    import dataclasses

    base = make_message(channel_id=200, msg_id=2024)
    msg = dataclasses.replace(
        base,
        reply_to_msg_id=99,
        forward_origin={
            "@type": "messageOriginChannel",
            "chat_id": -1001234567890,
            "message_id": 5,
            "author_signature": "Anon",
        },
        via_bot_user_id=777,
        media_album_id=1234567890,
        is_pinned=False,
    )
    await jsonl_repo.save_message(msg)
    rows = await jsonl_repo.list_messages(channel_ids=[200])
    got = next(r for r in rows if r.telegram_msg_id == 2024)
    assert got.reply_to_msg_id == 99
    assert got.forward_origin == {
        "@type": "messageOriginChannel",
        "chat_id": -1001234567890,
        "message_id": 5,
        "author_signature": "Anon",
    }
    assert got.via_bot_user_id == 777
    assert got.media_album_id == 1234567890
    assert got.is_pinned is False


# ---- PR #9:旧数据(无新字段)读出来 4 个新字段默认 None / False — 后端行为一致,可合并 ----


async def test_roundtrip_pr9_extra_fields_default_none(seeded_repo):
    """PR #9:旧数据(无新字段)读出来 4 个新字段都默认 None / False。

    backward-compat 测试 —— 如果 v1.3.0 的 .jsonl 文件被新代码读,
    新字段必须静默回退到默认值,不能 raise KeyError。
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
# 2026-09-10 v1.7.3:`update_message_pin` — in-mem / jsonl 行为不同(无 roundtrip vs roundtrip)
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


async def test_jsonl_update_pin_persists(jsonl_repo):
    """v1.7.3:Jsonl `update_message_pin` 写盘 → 重启后读回仍是 True。"""
    await jsonl_repo.save_message(make_message(channel_id=100, msg_id=1))
    await jsonl_repo.update_message_pin(100, 1, True)

    # roundtrip
    await jsonl_repo.close()
    store2 = JsonlFileStore(root=jsonl_repo._root)  # type: ignore[attr-defined]
    await store2.connect()
    msg = await store2.get_message(100, 1)
    assert msg is not None and msg.is_pinned is True
    await store2.close()
