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
    MEDIA_DONE_MIGRATION_KEY,
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


async def _migrate_subscribed_to_monitored(self: SqliteRepository) -> None:
    """2.0.6 一次性列改名迁移(迁移 channels.subscribed → monitored)。

    2026-11-25 P2-4:抽公共函数,init_schema 与 repair_schema 共用预检 —
    repair 路径此前缺这一步,会绕过主迁移补出**空的** monitored 列
    (ADD COLUMN),旧列数据留在 subscribed 里造成静默分裂。

    必须在 SCHEMA_SQL(IF NOT EXISTS)/ 幂等补列之前做 —— 否则旧表会先被
    补出一个空的 monitored 列,造成「旧列有数据、新列空」的静默分裂。
    调用方需已持 `self._lock`。
    """
    assert self._db is not None
    cols = {r["name"] for r in await self._db.execute_fetchall("PRAGMA table_info('channels')")}
    if "subscribed" in cols and "monitored" not in cols:
        log.warning("init_schema: 迁移 channels.subscribed → monitored")
        await self._db.execute("ALTER TABLE channels RENAME COLUMN subscribed TO monitored")


async def _run_media_done_migration_once(self: SqliteRepository) -> None:
    """2026-11-25 P2-3:媒体 done 历史迁移 — one-shot,经 meta 标记只跑一次。

    旧实现把 `UPDATE media SET download_status='done' WHERE object_key IS
    NOT NULL AND download_status='pending'` 内联在 SCHEMA_SQL 里每次启动
    无条件重跑 — 会复活用户显式重置为未下载、但 object_key 尚在的行。
    改为:meta 表无 `MEDIA_DONE_MIGRATION_KEY` 标记时执行一次并记标记;
    全新库(media 表空)同样只跑一次。调用方需已持 `self._lock`,且 meta
    表已存在(SCHEMA_SQL 之后执行)。直写 SQL 而不经 get_meta/set_meta —
    后者会再次抢 `self._lock` 死锁。
    """
    assert self._db is not None
    rows = await self._db.execute_fetchall(
        "SELECT value FROM meta WHERE key = ?", (MEDIA_DONE_MIGRATION_KEY,)
    )
    if rows:
        return
    await self._db.execute(
        "UPDATE media SET download_status = 'done' "
        "WHERE object_key IS NOT NULL AND download_status = 'pending'"
    )
    await self._db.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (MEDIA_DONE_MIGRATION_KEY, "1"),
    )


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
        await _migrate_subscribed_to_monitored(self)
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
        await _run_media_done_migration_once(self)


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
    """与 PG 同策略:missing_columns 补列;missing_tables 拒绝;其余仅 log。

    2026-11-25 P2-4:补列前先跑与 init_schema 同款的改名预检
    (迁移 channels.subscribed → monitored)— 否则旧库 repair 会绕过主迁移
    直接 ADD 出空的 monitored 列,监听状态静默清零。
    """
    assert self._db is not None
    if report.missing_tables:
        raise RuntimeError(
            f"schema repair: missing tables {report.missing_tables} — "
            "manual init_schema() required (auto-repair refuses DROP+CREATE)"
        )
    if report.missing_columns:
        async with self._lock:
            await _migrate_subscribed_to_monitored(self)
            for t, c, expected in report.missing_columns:
                # 改名预检可能已把 report 里的 subscribed→monitored 补上,
                # stale 条目不能再 ADD(否则 duplicate column)。
                cols = {
                    r["name"] for r in await self._db.execute_fetchall(f"PRAGMA table_info('{t}')")
                }
                if c in cols:
                    continue
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
