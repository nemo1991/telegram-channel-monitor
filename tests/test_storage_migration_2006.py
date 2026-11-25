"""v2.0.6 存储列改名迁移契约（FR-08）。

物理列 channels.subscribed(=监听)改名为 monitored：
- 旧库一次性 RENAME COLUMN,监听状态逐条保留;
- 新列默认值 SQLite/PG 一致(FALSE/0),DTO 映射默认值对齐;
- 自愈补列不会制造「旧列有数据、新列空」的静默分裂。
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

from tgmonitor.core.storage.sqlite_repo import EXPECTED_SCHEMA, SqliteRepository

# 迁移逻辑必须保留旧列名作为「源」;除此之外存储层不应再出现裸 subscribed 列。
_STORAGE_MODULES = (
    "sqlite_repo.py",
    # 2026-11-25 P2-N1:SQLite 按域拆出 free-function module,迁移 SQL 移到
    # _lifecycle_sqlite.py;全部纳入扫描以维持「无裸 subscribed 列」守门覆盖。
    "_lifecycle_sqlite.py",
    "_channels_sqlite.py",
    "_messages_sqlite.py",
    "_media_sqlite.py",
    "_thumbnails_sqlite.py",
    "_user_meta_sqlite.py",
    "_activity_sqlite.py",
    "_sqlite_common.py",
    "_lifecycle_pg.py",
    "_channels_pg.py",
    "_messages_pg.py",
    "_media_pg.py",
    "_thumbnails_pg.py",
    "_user_meta_pg.py",
    "_activity_pg.py",
)
_MIGRATION_MARKERS = (
    "RENAME COLUMN",  # ALTER TABLE ... RENAME COLUMN subscribed TO monitored
    "column_name=",  # information_schema 探测旧列是否存在
    "由 subscribed 正名",  # DDL 注释交代改名来历
    "迁移 channels.subscribed",  # log.warning 文案
)
_BARE_SUBSCRIBED = re.compile(r"(?<!tg_)\bsubscribed\b")


def test_no_legacy_physical_column_in_storage_sql() -> None:
    """SQL 字面量中不得再用 subscribed 物理列,防只在 PG integration 暴露漏改。"""
    root = Path(__file__).resolve().parents[1] / "src/tgmonitor/core/storage"
    offenders: list[str] = []
    for name in _STORAGE_MODULES:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value
            if text == "subscribed":  # 迁移探测用的哨兵字符串
                continue
            if _BARE_SUBSCRIBED.search(text) and not any(
                marker in text for marker in _MIGRATION_MARKERS
            ):
                offenders.append(f"{name}:{node.lineno}: {text.strip()[:80]}")
    assert not offenders, "物理列必须统一为 monitored:\n" + "\n".join(offenders)


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
