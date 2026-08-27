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
    SortDir,
    SortKey,
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


# ---- 2026-08-25 v1.3.0 PR #6:排序 + 分页 + count_media parity ----------


async def test_in_mem_list_media_sort_by_size_desc(in_mem_repo):
    """PR #6:SortKey.SIZE + SortDir.DESC — 大文件优先。"""
    rows = await in_mem_repo.list_media(
        sort=SortKey.SIZE, sort_dir=SortDir.DESC,
    )
    sizes = [r[2].file_size for r in rows]
    assert sizes == sorted(sizes, reverse=True)
    # 第一个是 video(5MB),后面是 photo(1KB)
    assert rows[0][2].type == MediaType.VIDEO


async def test_jsonl_list_media_sort_by_size_desc(jsonl_repo):
    """PR #6:Jsonl 后端 SortKey.SIZE + DESC parity。"""
    rows = await jsonl_repo.list_media(
        sort=SortKey.SIZE, sort_dir=SortDir.DESC,
    )
    sizes = [r[2].file_size for r in rows]
    assert sizes == sorted(sizes, reverse=True)
    assert rows[0][2].type == MediaType.VIDEO


async def test_in_mem_list_media_sort_by_status_asc(in_mem_repo):
    """PR #6:SortKey.STATUS + SortDir.ASC — 字典序排序(do<fa<pe<do w/loading)。"""
    rows = await in_mem_repo.list_media(
        sort=SortKey.STATUS, sort_dir=SortDir.ASC,
    )
    statuses = [r[2].download_status.value for r in rows]
    # 字典序 ASC:done<failed<pending
    assert statuses == sorted(statuses)
    assert rows[0][2].download_status == MediaDownloadStatus.DONE


async def test_jsonl_list_media_sort_by_status_asc(jsonl_repo):
    """PR #6:Jsonl SortKey.STATUS parity。"""
    rows = await jsonl_repo.list_media(
        sort=SortKey.STATUS, sort_dir=SortDir.ASC,
    )
    statuses = [r[2].download_status.value for r in rows]
    assert statuses == sorted(statuses)


async def test_in_mem_list_media_sort_by_date_desc_default(in_mem_repo):
    """PR #6:默认 sort=DATE / sort_dir=DESC(与 v1.2.0 既有行为对齐)。"""
    rows_default = await in_mem_repo.list_media()
    rows_explicit = await in_mem_repo.list_media(
        sort=SortKey.DATE, sort_dir=SortDir.DESC,
    )
    assert [r[2].object_key for r in rows_default] == [
        r[2].object_key for r in rows_explicit
    ]


async def test_jsonl_list_media_sort_by_date_desc_default(jsonl_repo):
    """PR #6:Jsonl 默认 DATE DESC 与显式 DATE DESC 等价。"""
    rows_default = await jsonl_repo.list_media()
    rows_explicit = await jsonl_repo.list_media(
        sort=SortKey.DATE, sort_dir=SortDir.DESC,
    )
    assert [r[2].object_key for r in rows_default] == [
        r[2].object_key for r in rows_explicit
    ]


async def test_in_mem_count_media_no_filter(in_mem_repo):
    """PR #6:无 filter 数全部 media — fixture 含 5 条。"""
    # msg1=1 photo + msg2=2(video + photo) + msg10=1 photo + msg5=1 video = 5
    n = await in_mem_repo.count_media()
    assert n == 5


async def test_jsonl_count_media_no_filter(jsonl_repo):
    """PR #6:Jsonl count_media 无 filter — fixture parity。"""
    n = await jsonl_repo.count_media()
    assert n == 5


async def test_in_mem_count_media_with_filter(in_mem_repo):
    """PR #6:count_media 应用 filter — status=DONE 只数下载完的。"""
    n = await in_mem_repo.count_media(status=MediaDownloadStatus.DONE)
    # DONE: photo_a(msg1) + clip_a(msg2) + photo_a(msg10) = 3
    assert n == 3


async def test_jsonl_count_media_with_filter(jsonl_repo):
    """PR #6:Jsonl count_media filter parity。"""
    n = await jsonl_repo.count_media(status=MediaDownloadStatus.DONE)
    assert n == 3


async def test_in_mem_count_media_pagination_consistency(in_mem_repo):
    """PR #6:sum(分页的 len) == count_media 总数。"""
    total = await in_mem_repo.count_media()
    seen = 0
    offset = 0
    limit = 2
    while True:
        rows = await in_mem_repo.list_media(limit=limit, offset=offset)
        if not rows:
            break
        seen += len(rows)
        offset += limit
    assert seen == total


async def test_jsonl_count_media_pagination_consistency(jsonl_repo):
    """PR #6:Jsonl 分页一致性 parity。"""
    total = await jsonl_repo.count_media()
    seen = 0
    offset = 0
    limit = 2
    while True:
        rows = await jsonl_repo.list_media(limit=limit, offset=offset)
        if not rows:
            break
        seen += len(rows)
        offset += limit
    assert seen == total


# ---- 2026-08-25 v1.3.0 PR #8:count_media_by_channel parity --------------


async def test_in_mem_count_media_by_channel_matches_fixture(in_mem_repo):
    """PR #8:InMemory count_media_by_channel = fixture 内 media 总数。

    fixture: ch100 msg1=1 photo + msg2=2 media + ch200 msg10=1 photo +
    ch300 msg5=1 video = 5
    """
    assert await in_mem_repo.count_media_by_channel(100) == 3  # 1 + 2
    assert await in_mem_repo.count_media_by_channel(200) == 1
    assert await in_mem_repo.count_media_by_channel(300) == 1
    assert await in_mem_repo.count_media_by_channel(999) == 0  # 空 channel


async def test_jsonl_count_media_by_channel_matches_fixture(jsonl_repo):
    """PR #8:Jsonl count_media_by_channel parity。"""
    assert await jsonl_repo.count_media_by_channel(100) == 3
    assert await jsonl_repo.count_media_by_channel(200) == 1
    assert await jsonl_repo.count_media_by_channel(300) == 1
    assert await jsonl_repo.count_media_by_channel(999) == 0


# ---------------------------------------------------------------------------
# 2026-08-27 v1.4.0 PR #12:list_messages 加 offset(导出真分页)— parity 测试
# ---------------------------------------------------------------------------


async def test_in_mem_list_messages_limit_offset_tail_pagination(in_mem_repo):
    """PR #12:offset 从尾部跳过 N 条再取 limit。

    fixture 4 条消息按 date asc 是 [msg1@1/1, msg2@1/2, msg10@1/3, msg5@1/4]。
    limit=2 offset=0 → 尾 2 条(升序)= [msg10, msg5]
    limit=2 offset=2 → 再往前 2 条(升序)= [msg1, msg2]
    limit=2 offset=4 → 空(数据耗尽)
    """
    page0 = await in_mem_repo.list_messages(
        channel_ids=[100, 200, 300], limit=2, offset=0,
    )
    assert [m.telegram_msg_id for m in page0] == [10, 5]
    page1 = await in_mem_repo.list_messages(
        channel_ids=[100, 200, 300], limit=2, offset=2,
    )
    assert [m.telegram_msg_id for m in page1] == [1, 2]
    page2 = await in_mem_repo.list_messages(
        channel_ids=[100, 200, 300], limit=2, offset=4,
    )
    assert page2 == []


async def test_jsonl_list_messages_limit_offset_tail_pagination(jsonl_repo):
    """PR #12:Jsonl parity — 与 InMemory 完全一致的 tail pagination 语义。"""
    page0 = await jsonl_repo.list_messages(
        channel_ids=[100, 200, 300], limit=2, offset=0,
    )
    assert [m.telegram_msg_id for m in page0] == [10, 5]
    page1 = await jsonl_repo.list_messages(
        channel_ids=[100, 200, 300], limit=2, offset=2,
    )
    assert [m.telegram_msg_id for m in page1] == [1, 2]
    page2 = await jsonl_repo.list_messages(
        channel_ids=[100, 200, 300], limit=2, offset=4,
    )
    assert page2 == []


async def test_in_mem_list_messages_offset_zero_unchanged(in_mem_repo):
    """PR #12:offset=0 必须与 v1.3.0 行为完全一致(向后兼容)。

    v1.3.0 list_messages 走 `out[-limit:]` —— fixture 4 条尾 2 条升序 =
    [msg10, msg5]。这条是基线回归保护。
    """
    rows = await in_mem_repo.list_messages(
        channel_ids=[100, 200, 300], limit=2, offset=0,
    )
    assert [m.telegram_msg_id for m in rows] == [10, 5]


async def test_jsonl_list_messages_offset_zero_unchanged(jsonl_repo):
    """PR #12:Jsonl offset=0 向后兼容。"""
    rows = await jsonl_repo.list_messages(
        channel_ids=[100, 200, 300], limit=2, offset=0,
    )
    assert [m.telegram_msg_id for m in rows] == [10, 5]


# ---------------------------------------------------------------------------
# 2026-08-27 v1.4.0 PR #9:MessageDTO 加 forward_origin / via_bot_user_id /
# media_album_id / is_pinned(reply_to_msg_id 已存在)— 后端 roundtrip parity。
# 只覆盖能跑的 InMemory + Jsonl 两后端,PG/Mongo 走自己 backend unit test。
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


async def test_in_mem_roundtrip_pr9_extra_fields_default_none(in_mem_repo):
    """PR #9:旧数据(无新字段)读出来 4 个新字段都默认 None / False。

    msg_id 用 4242 避开 fixture 已有消息。模拟 v1.3.0 时代的旧消息。
    """
    msg = make_message(channel_id=100, msg_id=4243)
    # make_message 用 base,没设新字段,默认值 = None / False
    await in_mem_repo.save_message(msg)
    rows = await in_mem_repo.list_messages(channel_ids=[100])
    got = next(r for r in rows if r.telegram_msg_id == 4243)
    assert got.reply_to_msg_id is None
    assert got.forward_origin is None
    assert got.via_bot_user_id is None
    assert got.media_album_id is None
    assert got.is_pinned is False


async def test_jsonl_roundtrip_pr9_extra_fields_default_none(jsonl_repo):
    """PR #9:Jsonl 旧数据读出来 4 个新字段都默认 None / False。

    backward-compat 测试 —— 如果 v1.3.0 的 .jsonl 文件被新代码读,
    新字段必须静默回退到默认值,不能 raise KeyError。
    """
    msg = make_message(channel_id=200, msg_id=2025)
    await jsonl_repo.save_message(msg)
    rows = await jsonl_repo.list_messages(channel_ids=[200])
    got = next(r for r in rows if r.telegram_msg_id == 2025)
    assert got.reply_to_msg_id is None
    assert got.forward_origin is None
    assert got.via_bot_user_id is None
    assert got.media_album_id is None
    assert got.is_pinned is False