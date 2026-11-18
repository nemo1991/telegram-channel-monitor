"""v2.0.6 存储列改名迁移契约（FR-08）。

物理列 channels.subscribed(=监听)改名为 monitored：
- 旧库一次性 RENAME COLUMN,监听状态逐条保留;
- 新列默认值 SQLite/PG 一致(FALSE/0),DTO 映射默认值对齐;
- 自愈补列不会制造「旧列有数据、新列空」的静默分裂。
"""

from __future__ import annotations

import sqlite3

from tgmonitor.core.storage.sqlite_repo import EXPECTED_SCHEMA, SqliteRepository


async def test_legacy_subscribed_column_migrates_with_data(tmp_path) -> None:
    """旧库 subscribed 列 + 已监听数据 → init_schema → monitored 列保留数据。"""
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            subscribed INTEGER NOT NULL DEFAULT 0,
            tg_subscribed INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO channels (id, title, subscribed, tg_subscribed)
        VALUES (1, '已监听频道', 1, 1), (2, '未监听频道', 0, 1);
        """
    )
    raw.commit()
    raw.close()

    repo = SqliteRepository(path=path)
    await repo.connect()
    try:
        await repo.init_schema()
        cols = {
            r["name"]
            for r in await repo._db.execute_fetchall("PRAGMA table_info('channels')")  # type: ignore[union-attr]
        }
        # 旧列已改名、新列存在、无新旧列分裂
        assert "monitored" in cols
        assert "subscribed" not in cols

        rows = await repo.list_monitored_channels()
        ids = {ch.id for ch in rows}
        assert ids == {1}, f"监听状态未逐条保留: {ids}"
        report = await repo.introspect_schema()
        assert report.ok is True
    finally:
        await repo.close()


async def test_new_db_monitored_defaults_to_false(tmp_path) -> None:
    """全新库 monitored 列为 NOT NULL DEFAULT 0,未设置时 is_monitored=False。"""
    path = tmp_path / "fresh.db"
    repo = SqliteRepository(path=path)
    await repo.connect()
    try:
        await repo.init_schema()
        assert EXPECTED_SCHEMA["channels"]["monitored"] == "INTEGER"
        # 显式造一条未指定 monitored 的行(占位路径也可),默认应为 0
        await repo._db.execute(  # type: ignore[union-attr]
            "INSERT INTO channels (id, title) VALUES (99, '默认频道')"
        )
        chans = await repo.list_channels()
        ch = next(c for c in chans if c.id == 99)
        assert ch.is_monitored is False
    finally:
        await repo.close()
