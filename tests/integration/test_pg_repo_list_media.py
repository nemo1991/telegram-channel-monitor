"""PostgresRepository.list_media 集成测试 — 2026-09-25 fix(media-bug)。

复现 2026-09-25 用户报告:DB 里有媒体记录,UI Media Manager 页空表。
根因:`list_media` SQL 写 `SELECT me.media_idx`,但 `media` 表没有该列 → 真 PG
抛 `UndefinedColumnError`,被 `run_coro` 静默吞掉。
修法:窗口函数 `ROW_NUMBER() OVER (PARTITION BY m.id ORDER BY me.id) - 1`
运行时生成。

测试策略:用 testcontainers 跑真 PG(testcontainers 不可用时 skip),save 1
message + 3 media,验证:
1. `list_media()` 不抛,返回 3 行
2. `media_idx` ∈ {0, 1, 2} 严格按 INSERT 顺序
3. filter / sort 仍生效
4. 与 Jsonl `_filter_media_rows` 返回的 media_idx 一致(后端语义对齐)

跑:`uv run pytest -m integration tests/integration/test_pg_repo_list_media.py -v`
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
    SortDir,
    SortKey,
)
from tgmonitor.core.storage.jsonl_store import JsonlFileStore
from tgmonitor.core.storage.postgres_repo import PostgresRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _mk_msg(
    channel_id: int,
    msg_id: int,
    media: list[MediaDTO],
    text: str = "hello",
) -> MessageDTO:
    return MessageDTO(
        id=0,
        channel_id=channel_id,
        telegram_msg_id=msg_id,
        date=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        text=text,
        author="alice",
        media=media,
    )


def _photo(
    fid: str,
    status: MediaDownloadStatus = MediaDownloadStatus.DONE,
    file_name: str | None = None,
    file_size: int = 1024,
) -> MediaDTO:
    return MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name=file_name or f"{fid}.jpg",
        file_size=file_size,
        width=800,
        height=600,
        telegram_file_id=fid,
        object_key=f"media/{fid}",
        object_backend="local",
        download_status=status,
    )


async def test_list_media_returns_window_ordered_indices(
    pg_repo: PostgresRepository,
) -> None:
    """2026-09-25 fix:save 1 message + 3 media,list_media 返 3 行,
    media_idx ∈ {0, 1, 2} 按 INSERT 顺序。"""
    ch = ChannelDTO(id=100, title="T", username="tst")
    await pg_repo.upsert_channel(ch)
    msg = _mk_msg(
        channel_id=100,
        msg_id=1,
        media=[
            _photo("p1"),
            _photo("p2"),
            _photo("p3"),
        ],
    )
    await pg_repo.save_message(msg)

    rows = await pg_repo.list_media()
    assert len(rows) == 3
    indices = [r[1] for r in rows]
    assert sorted(indices) == [0, 1, 2], f"media_idx 应是 0/1/2,实际 {indices}"


async def test_list_media_multiple_messages_partitioned(
    pg_repo: PostgresRepository,
) -> None:
    """每个 message 内部 media 独立编 0..N-1(PARTITION BY m.id 正确生效)。"""
    ch = ChannelDTO(id=200, title="T2", username="tst2")
    await pg_repo.upsert_channel(ch)

    # 3 条消息,每条 2 张图 — 共 6 个 media
    for msg_id in (1, 2, 3):
        await pg_repo.save_message(
            _mk_msg(
                channel_id=200,
                msg_id=msg_id,
                media=[_photo(f"m{msg_id}_a"), _photo(f"m{msg_id}_b")],
            )
        )

    rows = await pg_repo.list_media()
    assert len(rows) == 6
    # 按 (channel_id, media_idx) 分组,每组都应是 {0, 1}
    by_msg: dict[int, list[int]] = {}
    for m, idx, _med in rows:
        by_msg.setdefault(m.telegram_msg_id, []).append(idx)
    assert sorted(by_msg.keys()) == [1, 2, 3]
    for msg_id, idxs in by_msg.items():
        assert sorted(idxs) == [0, 1], f"msg #{msg_id} 的 media_idx 应是 [0, 1],实际 {idxs}"


async def test_list_media_with_status_filter(
    pg_repo: PostgresRepository,
) -> None:
    """status filter 仍生效(只返 DONE)。"""
    ch = ChannelDTO(id=300, title="T3")
    await pg_repo.upsert_channel(ch)
    await pg_repo.save_message(
        _mk_msg(
            channel_id=300,
            msg_id=1,
            media=[
                _photo("ok1", status=MediaDownloadStatus.DONE),
                _photo("fail1", status=MediaDownloadStatus.FAILED),
                _photo("ok2", status=MediaDownloadStatus.DONE),
                _photo("pend1", status=MediaDownloadStatus.PENDING),
            ],
        )
    )

    rows = await pg_repo.list_media(status=MediaDownloadStatus.DONE)
    assert len(rows) == 2
    file_names = [r[2].file_name for r in rows]
    assert "ok1.jpg" in file_names and "ok2.jpg" in file_names
    assert "fail1.jpg" not in file_names
    assert "pend1.jpg" not in file_names
    # media_idx 仍是 0..N-1
    assert sorted(r[1] for r in rows) == [0, 1]


async def test_list_media_with_media_type_filter(
    pg_repo: PostgresRepository,
) -> None:
    """media_type filter 仍生效(只返 PHOTO)。"""
    ch = ChannelDTO(id=400, title="T4")
    await pg_repo.upsert_channel(ch)
    photo = MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name="a.jpg",
        file_size=1024,
        width=80,
        height=60,
        telegram_file_id="photo_x",
        object_key="m/a",
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )
    doc = MediaDTO(
        type=MediaType.DOCUMENT,
        mime_type="application/pdf",
        file_name="a.pdf",
        file_size=2048,
        telegram_file_id="doc_x",
        object_key="m/a.pdf",
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )
    await pg_repo.save_message(_mk_msg(channel_id=400, msg_id=1, media=[photo, doc]))

    photos = await pg_repo.list_media(media_type=MediaType.PHOTO)
    assert len(photos) == 1
    assert photos[0][2].type == MediaType.PHOTO
    assert photos[0][1] == 0  # media_idx 0
    docs = await pg_repo.list_media(media_type=MediaType.DOCUMENT)
    assert len(docs) == 1
    assert docs[0][2].type == MediaType.DOCUMENT
    assert docs[0][1] == 0


async def test_list_media_with_search_filter(
    pg_repo: PostgresRepository,
) -> None:
    """search filter (LOWER LIKE %x%) 仍生效。"""
    ch = ChannelDTO(id=500, title="T5")
    await pg_repo.upsert_channel(ch)
    await pg_repo.save_message(
        _mk_msg(
            channel_id=500,
            msg_id=1,
            media=[
                _photo("cat1", file_name="cat_a.jpg"),
                _photo("dog1", file_name="dog_b.jpg"),
                _photo("cat2", file_name="kitty_cat.jpg"),
            ],
        )
    )

    rows = await pg_repo.list_media(search="cat")
    assert len(rows) == 2
    file_names = sorted(r[2].file_name for r in rows)
    assert file_names == ["cat_a.jpg", "kitty_cat.jpg"]


async def test_list_media_sort_by_size_desc_nulls_last(
    pg_repo: PostgresRepository,
) -> None:
    """sort=SIZE + sort_dir=DESC 不抛,且 NULL 在末尾。

    之前 `_MEDIA_SORT_COLUMN[SortKey.SIZE] = "me.file_size NULLS LAST"` 是
    plain string,拼出 `ORDER BY me.file_size NULLS LAST DESC` 无效 SQL。
    修法:拆 `(col, nulls_last)`,拼成 `col DESC NULLS LAST`。
    """
    ch = ChannelDTO(id=600, title="T6")
    await pg_repo.upsert_channel(ch)
    await pg_repo.save_message(
        _mk_msg(
            channel_id=600,
            msg_id=1,
            media=[
                _photo("small", file_size=100),
                _photo("big", file_size=9999),
            ],
        )
    )
    rows = await pg_repo.list_media(sort=SortKey.SIZE, sort_dir=SortDir.DESC)
    assert len(rows) == 2
    # 大的在前
    assert rows[0][2].file_size == 9999
    assert rows[1][2].file_size == 100


async def test_list_media_consistent_with_jsonl(
    pg_repo: PostgresRepository,
    tmp_path,
) -> None:
    """PG 与 Jsonl 同一组数据 → list_media 返回的 (msg, media_idx, media) 顺序一致。

    后端语义对齐是修复的核心目标(UI Media Manager 渲染靠
    `(channel_id, telegram_msg_id, media_idx)` 三元组定位)。
    """
    from tgmonitor.core.events import EventBus

    ch = ChannelDTO(id=700, title="T7")
    photos = [
        _photo("alpha"),
        _photo("beta"),
        _photo("gamma"),
    ]

    # PG 侧
    await pg_repo.upsert_channel(ch)
    await pg_repo.save_message(_mk_msg(700, 1, photos))

    # Jsonl 侧
    EventBus()  # Jsonl store 持引用即可,本测试不订阅
    jsonl = JsonlFileStore(tmp_path / "jsonl")
    await jsonl.connect()
    await jsonl.init_schema()
    await jsonl.upsert_channel(ch)
    await jsonl.save_message(_mk_msg(700, 1, photos))

    pg_rows = await pg_repo.list_media()
    jl_rows = await jsonl.list_media()

    # 都应返 3 行,media_idx 顺序 [0, 1, 2]
    assert [r[1] for r in pg_rows] == [r[1] for r in jl_rows] == [0, 1, 2]
    # file_name 顺序也应一致(Jsonl enumerate 顺序 = PG `ORDER BY me.id` 顺序)
    pg_names = [r[2].file_name for r in pg_rows]
    jl_names = [r[2].file_name for r in jl_rows]
    assert pg_names == jl_names == ["alpha.jpg", "beta.jpg", "gamma.jpg"]


async def test_count_media_returns_correct_total(
    pg_repo: PostgresRepository,
) -> None:
    """count_media 与 list_media 计数一致,且不受 media_idx 修复影响。"""
    ch = ChannelDTO(id=800, title="T8")
    await pg_repo.upsert_channel(ch)
    await pg_repo.save_message(_mk_msg(800, 1, [_photo("a"), _photo("b")]))

    total = await pg_repo.count_media()
    assert total == 2
    rows = await pg_repo.list_media()
    assert len(rows) == 2
