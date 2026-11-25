"""SQLite 生命周期子模块 — 2026-11-25 P2-N1 按域拆分。

持 6 个 lifecycle method（`connect` / `close` / `init_schema` /
`introspect_schema` / `repair_schema` / `ping`）。落地模式：free function +
`self` 透传（与 `_lifecycle_pg` 1:1 对齐）。`SqliteRepository` facade 持
`self._db` / `self._lock` / `self._path`，本 module 直接透传使用。

schema 常量（`SCHEMA_SQL` / `EXPECTED_SCHEMA` / `EXPECTED_TABLES`）与
`_sql_lower` 定义在 `_sqlite_common.py`，本 module 从那里 import。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from tgmonitor.core.storage._sqlite_common import (
    EXPECTED_SCHEMA,
    EXPECTED_TABLES,
    SCHEMA_SQL,
    _sql_lower,
)
from tgmonitor.core.storage.schema_report import ColumnDrift, SchemaReport

if TYPE_CHECKING:
    from tgmonitor.core.storage.sqlite_repo import SqliteRepository

log = logging.getLogger(__name__)


async def connect(self: SqliteRepository) -> None:
    """建连接 + PRAGMA;幂等(已连接直接返回)。"""
    if self._db is not None:
        return
    if isinstance(self._path, Path):
        self._path.parent.mkdir(parents=True, exist_ok=True)
    self._db = await aiosqlite.connect(str(self._path), isolation_level=None)
    self._db.row_factory = aiosqlite.Row
    await self._db.execute("PRAGMA journal_mode=WAL")
    await self._db.execute("PRAGMA foreign_keys=ON")
    await self._db.execute("PRAGMA busy_timeout=5000")
    await self._db.execute("PRAGMA synchronous=NORMAL")
    # Unicode LOWER — 对齐 PG(SQLite 内置 LOWER 只折 ASCII)。
    await self._db.create_function("LOWER", 1, _sql_lower, deterministic=True)


async def close(self: SqliteRepository) -> None:
    if self._db is not None:
        await self._db.close()
        self._db = None


async def init_schema(self: SqliteRepository) -> None:
    """建表 + 幂等补齐缺失列。

    2026-11-17 v2.0.3 修复:原实现只跑 `CREATE TABLE IF NOT EXISTS`,对存量表的
    **新增列无效**,存量库补不上 `tg_subscribed` 致大盘查询抛 "no such column"。
    这里对齐 PG 的 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`:每次 init_schema
    主动按 EXPECTED_SCHEMA 幂等补缺失列,新增列不回填(保持列默认值)。
    schema 迁移对用户无感 —— 无开关、无需配置,启动即自愈。
    """
    assert self._db is not None
    async with self._lock:
        # 2.0.6 一次性列改名迁移:旧库物理列 subscribed(=监听)→monitored。
        # 必须在 SCHEMA_SQL(IF NOT EXISTS)之前做 —— 否则旧表会先被补出
        # 一个空的 monitored 列,造成「旧列有数据、新列空」的静默分裂。
        cols = {r["name"] for r in await self._db.execute_fetchall("PRAGMA table_info('channels')")}
        if "subscribed" in cols and "monitored" not in cols:
            log.warning("init_schema: 迁移 channels.subscribed → monitored")
            await self._db.execute("ALTER TABLE channels RENAME COLUMN subscribed TO monitored")
        await self._db.executescript(SCHEMA_SQL)
        # 幂等补列 — 类型来自 EXPECTED_SCHEMA(与 SCHEMA_SQL 同步维护)。
        for table, columns in EXPECTED_SCHEMA.items():
            existing = {
                r["name"] for r in await self._db.execute_fetchall(f"PRAGMA table_info('{table}')")
            }
            for column, decl in columns.items():
                if column not in existing:
                    log.warning("init_schema: backfilling missing column %s.%s", table, column)
                    await self._db.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {decl}')


async def introspect_schema(self: SqliteRepository) -> SchemaReport:
    """查 sqlite_master + PRAGMA table_info 比对 EXPECTED_SCHEMA。"""
    assert self._db is not None
    async with self._lock:
        table_rows = await self._db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        actual_tables = {r["name"] for r in table_rows}
        actual_cols: dict[tuple[str, str], str] = {}
        for t in sorted(actual_tables):
            # 表名来自 sqlite_master(内部值),无注入面。
            rows = await self._db.execute_fetchall(f"PRAGMA table_info('{t}')")
            for r in rows:
                actual_cols[(t, r["name"])] = r["type"].upper()

    missing_tables = sorted(EXPECTED_TABLES - actual_tables)
    missing_columns: list[tuple[str, str, str]] = []
    wrong_types: list[ColumnDrift] = []
    extra_columns: list[tuple[str, str, str]] = []

    for t, cols in EXPECTED_SCHEMA.items():
        if t in missing_tables:
            continue
        for c, expected in cols.items():
            actual = actual_cols.get((t, c))
            if actual is None:
                missing_columns.append((t, c, expected))
            elif actual != expected:
                wrong_types.append(ColumnDrift(t, c, expected, actual))
    for (t, c), actual in actual_cols.items():
        if t in EXPECTED_SCHEMA and c not in EXPECTED_SCHEMA[t]:
            extra_columns.append((t, c, actual))

    return SchemaReport(
        missing_tables=missing_tables,
        missing_columns=missing_columns,
        wrong_types=wrong_types,
        extra_columns=extra_columns,
    )


async def repair_schema(self: SqliteRepository, report: SchemaReport) -> None:
    """与 PG 同策略:missing_columns 补列;missing_tables 拒绝;其余仅 log。"""
    assert self._db is not None
    if report.missing_tables:
        raise RuntimeError(
            f"schema repair: missing tables {report.missing_tables} — "
            "manual init_schema() required (auto-repair refuses DROP+CREATE)"
        )
    if report.missing_columns:
        async with self._lock:
            for t, c, expected in report.missing_columns:
                log.warning("schema repair: adding missing column %s.%s (%s)", t, c, expected)
                await self._db.execute(f'ALTER TABLE "{t}" ADD COLUMN "{c}" {expected}')
    if report.wrong_types:
        log.warning(
            "schema repair: %d column(s) have wrong types — NOT auto-fixed "
            "(manual ALTER COLUMN ... TYPE required)",
            len(report.wrong_types),
        )
    if report.extra_columns:
        log.info(
            "schema repair: %d extra column(s) — diagnostic only",
            len(report.extra_columns),
        )


async def ping(self: SqliteRepository) -> bool:
    if self._db is None:
        return False
    try:
        async with self._lock:
            await self._db.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 — ping 契约:任何异常一律返 False
        return False
