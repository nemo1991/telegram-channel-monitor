"""SqliteRepository 专属测试 — v2.0.0 默认后端。

跨后端行为契约在 `tests/test_storage_backends.py`(parametrize)与
`tests/test_thumbnail_repo_parity.py`;本文件只覆盖 SQLite 特有的点:

- EXPECTED_SCHEMA 与 SCHEMA_SQL 声明同步(守门,sqlite_repo.py 引用本测试)
- 落盘持久化:close → reopen roundtrip
- FK CASCADE:delete_channel / delete_message 带走 media + thumbnails
- FK 拒绝孤儿 thumbnail(与 PG 同约束)
- introspect / repair:missing column 补列、missing table 拒绝、extra 仅诊断
- 单连接 + asyncio.Lock:并发 save_message 不交错
- datetime naive/aware round-trip、LIKE 转义、Unicode LOWER
- WAL / 连接生命周期
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from tests.fixtures._factories import make_message
from tgmonitor.core.dto import (
    ChannelDTO,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    ThumbnailDTO,
)
from tgmonitor.core.storage.sqlite_repo import (
    EXPECTED_SCHEMA,
    SqliteRepository,
)

# ---- fixtures ----


@pytest_asyncio.fixture
async def repo(tmp_path) -> SqliteRepository:
    r = SqliteRepository(path=tmp_path / "sqlite_repo_test.db")
    await r.connect()
    await r.init_schema()
    try:
        yield r
    finally:
        await r.close()


def _photo(object_key: str | None = "media/p.jpg", fid: str | None = None) -> MediaDTO:
    return MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name="p.jpg",
        file_size=1024,
        object_key=object_key,
        object_backend="local",
        download_status=MediaDownloadStatus.DONE if object_key else MediaDownloadStatus.PENDING,
        telegram_file_id=fid,
    )


def _thumb(channel_id: int = 100, msg_id: int = 1, idx: int = 0) -> ThumbnailDTO:
    return ThumbnailDTO(
        channel_id=channel_id,
        telegram_msg_id=msg_id,
        media_idx=idx,
        object_key=f"thumb/{channel_id}-{msg_id}-{idx}.jpg",
        object_backend="local",
        file_size=100,
        download_status=MediaDownloadStatus.DONE,
    )


# ---- EXPECTED_SCHEMA 守门 ----


async def test_expected_schema_matches_ddl(repo: SqliteRepository) -> None:
    """EXPECTED_SCHEMA 必须与 SCHEMA_SQL 实际建出的列/类型完全一致。

    改 DDL 忘改 EXPECTED_SCHEMA(或反之)→ 本测试红;introspect_schema
    依赖这份对照,漏同步会让启动期 drift 检测失真。
    """
    assert repo._db is not None
    rows = await repo._db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    actual_tables = {r["name"] for r in rows}
    assert actual_tables == set(EXPECTED_SCHEMA.keys())
    for table, expected_cols in EXPECTED_SCHEMA.items():
        col_rows = await repo._db.execute_fetchall(f"PRAGMA table_info('{table}')")
        actual_cols = {r["name"]: r["type"].upper() for r in col_rows}
        assert actual_cols == expected_cols, f"table {table} drift"


# ---- 持久化 ----


async def test_persistence_close_reopen_roundtrip(tmp_path) -> None:
    """channel / message+media / thumbnail / meta 全部落盘,重开可读。"""
    path = tmp_path / "persist.db"
    r1 = SqliteRepository(path=path)
    await r1.connect()
    await r1.init_schema()
    await r1.upsert_channel(ChannelDTO(id=100, title="Persist", is_subscribed=True))
    await r1.save_message(
        make_message(channel_id=100, msg_id=1, text="存盘", media=[_photo(fid="f-1")])
    )
    await r1.save_thumbnail(_thumb())
    await r1.set_meta("schema_version", "2")
    await r1.close()

    r2 = SqliteRepository(path=path)
    await r2.connect()
    try:
        ch = await r2.get_channel(100)
        assert ch is not None
        assert ch.title == "Persist"
        assert ch.is_subscribed is True
        msg = await r2.get_message(100, 1)
        assert msg is not None
        assert msg.text == "存盘"
        assert len(msg.media) == 1
        assert msg.media[0].object_key == "media/p.jpg"
        assert msg.media[0].telegram_file_id == "f-1"
        th = await r2.get_thumbnail(100, 1, 0)
        assert th is not None
        assert th.object_key == "thumb/100-1-0.jpg"
        assert await r2.get_meta("schema_version") == "2"
    finally:
        await r2.close()


async def test_connect_creates_parent_dirs_and_idempotent(tmp_path) -> None:
    path = tmp_path / "nested" / "dirs" / "x.db"
    r = SqliteRepository(path=path)
    await r.connect()
    await r.connect()  # 幂等,不重建连接
    try:
        assert path.exists()
        assert await r.ping() is True
    finally:
        await r.close()


async def test_wal_mode_enabled(repo: SqliteRepository) -> None:
    assert repo._db is not None
    cur = await repo._db.execute("PRAGMA journal_mode")
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "wal"


async def test_in_memory_backend() -> None:
    r = SqliteRepository(path=":memory:")
    await r.connect()
    await r.init_schema()
    try:
        await r.save_message(make_message(channel_id=1, msg_id=1))
        assert await r.get_message(1, 1) is not None
    finally:
        await r.close()


# ---- FK 约束 ----


async def test_delete_channel_cascades_everything(repo: SqliteRepository) -> None:
    """FK CASCADE:删频道 → messages / media / thumbnails 全清。"""
    await repo.save_message(
        make_message(channel_id=100, msg_id=1, media=[_photo(), _photo("media/q.jpg")])
    )
    await repo.save_thumbnail(_thumb(100, 1, 0))
    await repo.save_thumbnail(_thumb(100, 1, 1))
    # 异频道不受影响
    await repo.save_message(make_message(channel_id=200, msg_id=1, media=[_photo()]))

    await repo.delete_channel(100)

    assert await repo.get_channel(100) is None
    assert await repo.get_message(100, 1) is None
    assert await repo.count_media(channel_ids=[100]) == 0
    assert await repo.count_thumbnails_by_object_key("thumb/100-1-0.jpg") == 0
    assert await repo.count_thumbnails_by_object_key("thumb/100-1-1.jpg") == 0
    # 异频道完好
    assert await repo.get_message(200, 1) is not None
    assert await repo.count_media(channel_ids=[200]) == 1


async def test_delete_message_cascades_media_and_thumbs(repo: SqliteRepository) -> None:
    await repo.save_message(make_message(channel_id=100, msg_id=1, media=[_photo()]))
    await repo.save_thumbnail(_thumb())
    await repo.delete_message(100, 1)
    assert await repo.count_media(channel_ids=[100]) == 0
    assert await repo.get_thumbnail(100, 1, 0) is None


async def test_fk_rejects_orphan_thumbnail(repo: SqliteRepository) -> None:
    """父消息不存在 → save_thumbnail 抛 IntegrityError(与 PG 同约束)。"""
    with pytest.raises(sqlite3.IntegrityError):
        await repo.save_thumbnail(_thumb(999, 999, 0))


# ---- introspect / repair ----


async def test_introspect_ok_after_init(repo: SqliteRepository) -> None:
    report = await repo.introspect_schema()
    assert report.ok is True
    assert report.summary() == "ok"


async def test_repair_adds_missing_column(repo: SqliteRepository) -> None:
    assert repo._db is not None
    await repo._db.execute("ALTER TABLE meta DROP COLUMN value")
    report = await repo.introspect_schema()
    assert report.ok is False
    assert ("meta", "value", "TEXT") in report.missing_columns

    await repo.repair_schema(report)

    after = await repo.introspect_schema()
    assert after.ok is True
    # 补回的列可正常读写
    await repo.set_meta("k", "v")
    assert await repo.get_meta("k") == "v"


async def test_repair_refuses_missing_table(repo: SqliteRepository) -> None:
    assert repo._db is not None
    await repo._db.execute("DROP TABLE meta")
    report = await repo.introspect_schema()
    assert report.missing_tables == ["meta"]
    with pytest.raises(RuntimeError, match="missing tables"):
        await repo.repair_schema(report)


async def test_extra_column_diagnostic_only(repo: SqliteRepository) -> None:
    assert repo._db is not None
    await repo._db.execute("ALTER TABLE channels ADD COLUMN legacy_note TEXT")
    report = await repo.introspect_schema()
    assert report.ok is True  # extra 不阻塞启动
    assert ("channels", "legacy_note", "TEXT") in report.extra_columns
    await repo.repair_schema(report)  # 仅 log,不删列
    cols = await repo._db.execute_fetchall("PRAGMA table_info('channels')")
    assert any(c["name"] == "legacy_note" for c in cols)


# ---- 并发 ----


async def test_concurrent_save_message_serialized(repo: SqliteRepository) -> None:
    """asyncio.gather 并发写 — 单连接 + Lock 串行化,不交错事务、pk 唯一。"""
    msgs = [make_message(channel_id=i % 3, msg_id=i, text=f"m{i}") for i in range(30)]
    ids = await asyncio.gather(*(repo.save_message(m) for m in msgs))
    assert len(set(ids)) == 30
    assert await repo.count_messages(0) == 10
    assert await repo.count_messages(1) == 10
    assert await repo.count_messages(2) == 10


async def test_concurrent_reads_and_writes(repo: SqliteRepository) -> None:
    """读写混跑 — 读不会撞见 BEGIN 中间态(锁覆盖读路径)。"""
    await repo.save_message(make_message(channel_id=1, msg_id=0, media=[_photo()]))

    async def writer(i: int) -> None:
        await repo.save_message(make_message(channel_id=1, msg_id=i, media=[_photo()]))

    async def reader() -> int:
        return await repo.count_media(channel_ids=[1])

    tasks = [writer(i) for i in range(1, 11)] + [reader() for _ in range(10)]
    results = await asyncio.gather(*tasks)
    counts = [r for r in results if isinstance(r, int)]
    assert all(1 <= c <= 11 for c in counts)
    assert await repo.count_media(channel_ids=[1]) == 11


# ---- 序列化语义 ----


async def test_datetime_roundtrip_naive_and_aware(repo: SqliteRepository) -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)
    aware_utc = datetime(2026, 1, 2, 4, 0, 0, tzinfo=UTC)
    aware_offset = datetime(2026, 1, 3, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    await repo.save_message(make_message(channel_id=1, msg_id=1, date=naive))
    await repo.save_message(make_message(channel_id=1, msg_id=2, date=aware_utc))
    await repo.save_message(make_message(channel_id=1, msg_id=3, date=aware_offset))

    m1 = await repo.get_message(1, 1)
    m2 = await repo.get_message(1, 2)
    m3 = await repo.get_message(1, 3)
    assert m1 is not None and m1.date == naive
    assert m1.date.tzinfo is None
    assert m2 is not None and m2.date == aware_utc
    assert m3 is not None and m3.date == aware_offset
    assert m3.date.utcoffset() == timedelta(0)  # aware 统一归 UTC 存


async def test_save_message_upsert_stable_pk_and_media_replace(repo: SqliteRepository) -> None:
    pk1 = await repo.save_message(
        make_message(channel_id=1, msg_id=1, media=[_photo("media/a.jpg"), _photo("media/b.jpg")])
    )
    pk2 = await repo.save_message(
        make_message(channel_id=1, msg_id=1, text="v2", media=[_photo("media/c.jpg")])
    )
    assert pk1 == pk2  # upsert 不换 pk
    msg = await repo.get_message(1, 1)
    assert msg is not None
    assert msg.text == "v2"
    assert [m.object_key for m in msg.media] == ["media/c.jpg"]  # 旧 media 清掉
    assert await repo.count_media(channel_ids=[1]) == 1


async def test_list_messages_search_escapes_like_wildcards(repo: SqliteRepository) -> None:
    """list_messages 的 LIKE 转义(与 PG 对齐):% / _ 按字面匹配。"""
    await repo.save_message(make_message(channel_id=1, msg_id=1, text="discount 50% off"))
    await repo.save_message(make_message(channel_id=1, msg_id=2, text="plain text"))
    await repo.save_message(make_message(channel_id=1, msg_id=3, text="snake_case_name"))

    pct = await repo.list_messages([1], search="%")
    assert [m.telegram_msg_id for m in pct] == [1]
    underscore = await repo.list_messages([1], search="_")
    assert [m.telegram_msg_id for m in underscore] == [3]


async def test_media_search_unicode_lower(repo: SqliteRepository) -> None:
    """LOWER 覆写为 Python str.lower — 非 ASCII 大小写折叠与 PG 对齐。"""
    await repo.save_message(
        make_message(
            channel_id=1,
            msg_id=1,
            media=[
                MediaDTO(
                    type=MediaType.PHOTO,
                    file_name="CAFÉ.jpg",
                    download_status=MediaDownloadStatus.DONE,
                    object_key="media/cafe.jpg",
                )
            ],
        )
    )
    rows = await repo.list_media(search="café")
    assert len(rows) == 1
    assert rows[0][2].file_name == "CAFÉ.jpg"


async def test_meta_roundtrip_and_overwrite(repo: SqliteRepository) -> None:
    assert await repo.get_meta("missing") is None
    await repo.set_meta("k", "v1")
    await repo.set_meta("k", "v2")
    assert await repo.get_meta("k") == "v2"


# ---- 生命周期 ----


async def test_ping_lifecycle(tmp_path) -> None:
    r = SqliteRepository(path=tmp_path / "ping.db")
    assert await r.ping() is False  # 未连接
    await r.connect()
    assert await r.ping() is True
    await r.close()
    assert await r.ping() is False  # 已关闭


async def test_db_file_created_at_path(tmp_path) -> None:
    path = Path(tmp_path) / "located.db"
    r = SqliteRepository(path=path)
    await r.connect()
    try:
        assert path.is_file()
    finally:
        await r.close()
