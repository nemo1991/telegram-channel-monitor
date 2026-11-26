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
#
# 2026-11-26 2.1.7:扫描范围从手写清单改为按目录自动发现。手写清单 16 个、
# 目录实有 22 个 —— 2.1.3 N1 拆分后新增的模块若忘了登记,守卫就对它静默失守
# (这正是本守护在 2.1.3 期间踩过的坑)。数量下限防止目录被误挪空后守卫变绿。
_STORAGE_DIR = Path(__file__).resolve().parents[1] / "src/tgmonitor/core/storage"
_STORAGE_MIN_MODULES = 20
_MIGRATION_MARKERS = (
    "RENAME COLUMN",  # ALTER TABLE ... RENAME COLUMN subscribed TO monitored
    "column_name=",  # information_schema 探测旧列是否存在
    "由 subscribed 正名",  # DDL 注释交代改名来历
    "迁移 channels.subscribed",  # log.warning 文案
)
_BARE_SUBSCRIBED = re.compile(r"(?<!tg_)\bsubscribed\b")


def test_storage_module_inventory_is_complete() -> None:
    """守卫自身的元测试:目录被误挪 / 大批模块被移走时,下面的 SQL 守卫必须先红。"""
    modules = sorted(_STORAGE_DIR.glob("*.py"))
    assert len(modules) >= _STORAGE_MIN_MODULES, (
        f"core/storage 只找到 {len(modules)} 个模块(下限 {_STORAGE_MIN_MODULES}),"
        "目录结构变了请同步核对 test_no_legacy_physical_column_in_storage_sql"
    )
    # 两类实现都必须在扫描范围内,缺一类就是单后端盲区
    names = {p.name for p in modules}
    assert {
        "sqlite_repo.py",
        "postgres_repo.py",
        "_lifecycle_sqlite.py",
        "_lifecycle_pg.py",
    } <= names


def test_no_legacy_physical_column_in_storage_sql() -> None:
    """SQL 字面量中不得再用 subscribed 物理列,防只在 PG integration 暴露漏改。"""
    offenders: list[str] = []
    for path in sorted(_STORAGE_DIR.glob("*.py")):
        name = path.name
        tree = ast.parse(path.read_text(encoding="utf-8"))
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
