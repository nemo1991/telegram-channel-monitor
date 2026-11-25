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
from tgmonitor.core.storage._sqlite_common import MEDIA_DONE_MIGRATION_KEY
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
        download_status=MediaDownloadStatus.DONE if object_key else None,
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
    await r1.upsert_channel(ChannelDTO(id=100, title="Persist", is_monitored=True))
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
        assert ch.is_monitored is True
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


async def test_init_schema_backfills_new_columns_on_legacy_db(tmp_path) -> None:
    """存量库(缺 tg_subscribed 等新列)仅靠 init_schema 就能补齐。

    回归 2026-11-17 线上报错:旧库 channels 表无 tg_subscribed 列,
    SCHEMA_SQL 的 CREATE TABLE IF NOT EXISTS 对存量表无效 → 列永远补不上,
    大盘 count_tg_subscribed() 抛 "no such column: tg_subscribed"。
    修复后 init_schema() 无条件幂等补列,与 PG 的 ALTER ... IF NOT EXISTS 对齐;
    schema 自愈对用户无感,无开关、无需配置。
    """
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            monitored INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO channels (id, title, monitored) VALUES (42, '旧频道', 0);
        """
    )
    raw.commit()
    raw.close()

    repo = SqliteRepository(path=path)
    await repo.connect()
    try:
        await repo.init_schema()

        cols = await repo._db.execute_fetchall("PRAGMA table_info('channels')")  # type: ignore[union-attr]
        names = {c["name"] for c in cols}
        assert "tg_subscribed" in names
        assert EXPECTED_SCHEMA["channels"]["tg_subscribed"] == "INTEGER"

        # 关键查询不再抛错;存量行未回填 → TG 订阅数为 0
        assert await repo.count_tg_subscribed() == 0
        report = await repo.introspect_schema()
        assert report.ok is True
    finally:
        await repo.close()


async def test_init_schema_backfill_is_idempotent(tmp_path) -> None:
    """对已是最新 schema 的库重复 init_schema 不报错、不加重复列。"""
    path = tmp_path / "fresh.db"
    repo = SqliteRepository(path=path)
    await repo.connect()
    try:
        await repo.init_schema()
        await repo.init_schema()  # 二次执行
        cols = await repo._db.execute_fetchall("PRAGMA table_info('channels')")  # type: ignore[union-attr]
        names = [c["name"] for c in cols]
        assert names.count("tg_subscribed") == 1
    finally:
        await repo.close()


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


async def test_save_message_upsert_preserves_user_annotations(repo: SqliteRepository) -> None:
    """P1-2 回归(2026-11-25):save_message upsert 不得覆盖用户标注。

    旧行为:ON CONFLICT DO UPDATE 列表含 is_favorite / tags / notes —
    monitor / sync 对已落库消息重复推送时带 TG 侧默认空值,把用户标注
    静默清零。用户标注的唯一落库路径是 set_favorite / set_tags /
    set_notes 专用方法;save_message upsert 只更新 TG 侧字段。

    显式清空走专用方法(如 set_favorite(False)),路径仍然有效。
    """
    await repo.save_message(
        make_message(
            channel_id=1,
            msg_id=1,
            text="v1",
            is_favorite=True,
            tags=["tech"],
            notes="user note",
        )
    )
    # 模拟 monitor / sync 对已落库消息的重复推送(TG 重映射 DTO,标注为默认空值)
    await repo.save_message(make_message(channel_id=1, msg_id=1, text="v2"))
    msg = await repo.get_message(1, 1)
    assert msg is not None
    assert msg.text == "v2", "TG 侧字段正常覆盖"
    assert msg.is_favorite is True, "is_favorite 被 upsert 清零(P1-2)"
    assert msg.tags == ["tech"], "tags 被 upsert 清零(P1-2)"
    assert msg.notes == "user note", "notes 被 upsert 清零(P1-2)"

    # 显式清空走专用方法,路径有效
    await repo.set_favorite(1, 1, False)
    await repo.set_tags(1, 1, [])
    await repo.set_notes(1, 1, "")
    msg = await repo.get_message(1, 1)
    assert msg is not None
    assert msg.is_favorite is False
    assert msg.tags == []
    assert msg.notes == ""


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


# ---- P2 回归(2026-11-25)----


async def test_media_search_escapes_like_wildcards(repo: SqliteRepository) -> None:
    """P2-1 回归:list_media 的 search 必须转义 % / _ / \\(与 list_messages
    的正确实现一致)。旧实现 `%{search}%` 直拼,`_` 当单字符通配符 —
    search="a_c" 会误命中 "abc.jpg"。"""
    await repo.save_message(
        make_message(
            channel_id=1,
            msg_id=1,
            media=[
                MediaDTO(
                    type=MediaType.PHOTO,
                    file_name="a_c.jpg",
                    download_status=MediaDownloadStatus.DONE,
                    object_key="media/a_c.jpg",
                )
            ],
        )
    )
    await repo.save_message(
        make_message(
            channel_id=1,
            msg_id=2,
            media=[
                MediaDTO(
                    type=MediaType.PHOTO,
                    file_name="abc.jpg",
                    download_status=MediaDownloadStatus.DONE,
                    object_key="media/abc.jpg",
                )
            ],
        )
    )
    await repo.save_message(
        make_message(
            channel_id=1,
            msg_id=3,
            media=[
                MediaDTO(
                    type=MediaType.PHOTO,
                    file_name="100%.jpg",
                    download_status=MediaDownloadStatus.DONE,
                    object_key="media/100%.jpg",
                )
            ],
        )
    )

    rows = await repo.list_media(search="a_c")
    assert [r[2].file_name for r in rows] == ["a_c.jpg"], "`_` 必须按字面匹配"
    rows = await repo.list_media(search="100%")
    assert [r[2].file_name for r in rows] == ["100%.jpg"], "`%` 必须按字面匹配"


async def test_media_done_migration_is_one_shot(repo: SqliteRepository) -> None:
    """P2-3 回归:媒体 done 迁移经 meta 标记只跑一次。

    旧实现把 `UPDATE media SET download_status='done' ...` 内联在
    SCHEMA_SQL 里每次启动无条件重跑 — 用户显式重置为未下载但 object_key
    仍在的行会被复活。"""
    assert repo._db is not None
    await repo.save_message(make_message(channel_id=1, msg_id=1, media=[_photo()]))
    # 首次 init_schema(fixture 已跑)已写标记
    assert await repo.get_meta(MEDIA_DONE_MIGRATION_KEY) == "1"

    # 模拟用户重置:object_key 仍在、状态回到未下载 → 再次 init_schema 不得复活
    await repo._db.execute(
        "UPDATE media SET download_status = 'pending' WHERE object_key IS NOT NULL"
    )
    await repo.init_schema()
    rows = await repo._db.execute_fetchall(
        "SELECT download_status FROM media WHERE object_key IS NOT NULL"
    )
    assert all(r["download_status"] == "pending" for r in rows), (
        "one-shot 标记已存在,重复 init_schema 不得把 pending 行复活为 done"
    )

    # 删掉标记(模拟首次升级旧库)→ 再跑一次迁移应执行并转换 + 补标记
    await repo._db.execute("DELETE FROM meta WHERE key = ?", (MEDIA_DONE_MIGRATION_KEY,))
    await repo.init_schema()
    rows = await repo._db.execute_fetchall(
        "SELECT download_status FROM media WHERE object_key IS NOT NULL"
    )
    assert all(r["download_status"] == "done" for r in rows)
    assert await repo.get_meta(MEDIA_DONE_MIGRATION_KEY) == "1"


async def test_delete_messages_chunks_over_sqlite_variable_limit(repo: SqliteRepository) -> None:
    """P2-6 回归:delete_messages 超过 SQLite 32766 变量上限的大选区不抛错。

    旧实现单发 IN(39999 个占位符)→ "too many SQL variables";修复后按
    ≤1000/批分批,与 PG(ANY(array),无上限)行为对齐。
    """
    await repo.save_message(make_message(channel_id=1, msg_id=1))
    # 39999 个 id(含真实存在的 1)——超过 SQLite 变量上限
    await repo.delete_messages(1, list(range(1, 40000)))
    assert await repo.get_message(1, 1) is None


async def test_repair_schema_runs_subscribed_rename_first(tmp_path) -> None:
    """P2-4 回归:repair_schema 补列前必须先跑 subscribed→monitored 改名预检。

    旧实现 repair 只 ADD COLUMN monitored(空列),旧列数据留在 subscribed
    里造成「旧列有数据、新列空」的静默分裂 — 监听状态清零。

    构造:先 init_schema 建全量表,再手动把 channels 列降级回旧名
    (RENAME monitored→subscribed + DROP 新增列),模拟绕过主迁移的旧库。
    """
    path = tmp_path / "legacy_repair.db"
    repo = SqliteRepository(path=path)
    await repo.connect()
    try:
        assert repo._db is not None
        await repo.init_schema()
        await repo.upsert_channel(ChannelDTO(id=1, title="已监听频道", is_monitored=True))
        await repo.upsert_channel(ChannelDTO(id=2, title="未监听频道", is_monitored=False))
        # 模拟旧库:监听数据回到 subscribed 列,monitored 列消失
        await repo._db.execute("ALTER TABLE channels RENAME COLUMN monitored TO subscribed")

        report = await repo.introspect_schema()
        assert report.ok is False
        await repo.repair_schema(report)

        cols = {r["name"] for r in await repo._db.execute_fetchall("PRAGMA table_info('channels')")}
        assert "monitored" in cols
        assert "subscribed" not in cols, "repair 必须改名而非补出空 monitored 列"
        ch = await repo.get_channel(1)
        assert ch is not None and ch.is_monitored is True, "监听状态必须随改名逐条保留"
        ch2 = await repo.get_channel(2)
        assert ch2 is not None and ch2.is_monitored is False
        after = await repo.introspect_schema()
        assert after.ok is True
    finally:
        await repo.close()


# ---- P2 #36: update_message_media 定向 UPDATE(2026-11-25)----


async def test_update_message_media_replaces_media_rows_keeps_text(repo: SqliteRepository):
    """P2 #36:定向 UPDATE 只重写 media 行,text / views / 用户标注不动。

    旧路径 save_message 整条覆盖会把下载期间并发编辑的 text 覆盖回去。
    """
    from tests.fixtures._factories import make_message as _mk

    msg = _mk(channel_id=100, msg_id=1, text="原文")
    msg.media = [_photo(object_key=None, fid="fid-1")]
    await repo.save_message(msg)
    await repo.set_tags(100, 1, ["important"])
    await repo.set_favorite(100, 1, True)

    # 下载完成:media 侧字段更新(上传回写 object_key / DONE)
    done_media = [
        MediaDTO(
            type=MediaType.PHOTO,
            mime_type="image/jpeg",
            file_name="p.jpg",
            file_size=1024,
            object_key="media/p.jpg",
            object_backend="local",
            download_status=MediaDownloadStatus.DONE,
            telegram_file_id="fid-1",
        )
    ]
    await repo.update_message_media(100, 1, done_media)

    stored = await repo.get_message(100, 1)
    assert stored is not None
    assert stored.text == "原文", "text 不得被下载回写覆盖(P2 #36)"
    assert stored.tags == ["important"], "用户标注不得被下载回写清零"
    assert stored.is_favorite is True
    assert len(stored.media) == 1
    assert stored.media[0].download_status == MediaDownloadStatus.DONE
    assert stored.media[0].object_key == "media/p.jpg"

    # 再来一轮:media 行确实是 DELETE+重插(列表变化生效)
    await repo.update_message_media(100, 1, [])
    stored2 = await repo.get_message(100, 1)
    assert stored2 is not None
    assert stored2.media == []
    assert stored2.text == "原文", "media 清空也不得波及 text"


async def test_update_message_media_missing_message_is_noop(repo: SqliteRepository):
    """消息不存在(下载期间被删)→ no-op 不抛(idempotent)。"""
    await repo.update_message_media(999, 42, [_photo()])


# ---- P2 #37: 频道原子写路径(2026-11-25)----


async def test_upsert_channel_and_set_monitored_forces_flag(repo: SqliteRepository):
    """P2 #37:caller DTO 的 is_monitored=False 也会被强制置 True(单语句原子)。"""
    await repo.upsert_channel_and_set_monitored(ChannelDTO(id=7, title="频道", is_monitored=False))
    ch = await repo.get_channel(7)
    assert ch is not None
    assert ch.is_monitored is True
    assert ch.title == "频道"
    # 已存在行:再次 upsert 仍保持 monitored=True
    await repo.upsert_channel_and_set_monitored(ChannelDTO(id=7, title="改名"))
    ch2 = await repo.get_channel(7)
    assert ch2 is not None
    assert ch2.is_monitored is True
    assert ch2.title == "改名"


async def test_upsert_channel_metadata_and_set_tg_subscribed(repo: SqliteRepository):
    """P2 #37:metadata upsert + tg_subscribed=True;monitored 保持旧值。"""
    await repo.upsert_channel(ChannelDTO(id=8, title="旧", is_monitored=True))
    await repo.upsert_channel_metadata_and_set_tg_subscribed(
        ChannelDTO(id=8, title="新", is_monitored=False, is_tg_subscribed=False)
    )
    ch = await repo.get_channel(8)
    assert ch is not None
    assert ch.title == "新"
    assert ch.is_tg_subscribed is True
    assert ch.is_monitored is True, "monitored 标志不得被 metadata upsert 触碰"


async def test_set_channel_flags_single_call(repo: SqliteRepository):
    """P2 #37:双标志一次落库;None=不动;未建档 stub。"""
    await repo.upsert_channel(ChannelDTO(id=9, title="c", is_monitored=True))
    await repo.set_channel_flags(9, tg_subscribed=False, monitored=False)
    ch = await repo.get_channel(9)
    assert ch is not None
    assert ch.is_tg_subscribed is False
    assert ch.is_monitored is False

    # 只动 tg_subscribed,monitored 保持
    await repo.set_channel_flags(9, tg_subscribed=True)
    ch2 = await repo.get_channel(9)
    assert ch2 is not None
    assert ch2.is_tg_subscribed is True
    assert ch2.is_monitored is False

    # 未建档:id-stub
    await repo.set_channel_flags(77, monitored=True)
    ch3 = await repo.get_channel(77)
    assert ch3 is not None and ch3.is_monitored is True
