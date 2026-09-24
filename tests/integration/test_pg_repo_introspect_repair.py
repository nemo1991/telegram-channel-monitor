"""PostgresRepository introspect_schema / repair_schema — 真 PG 集成测试。

2026-09-23 v1.8.x:用 testcontainers[postgresql] 起真 PG,跑
- fresh DB → ok=True
- DROP 一列 → missing_columns
- ALTER COLUMN TYPE → wrong_types(repair 不动,只 log)
- repair ADD COLUMN IF NOT EXISTS → 二次 introspect ok=True
- repair 幂等
- DROP TABLE → repair 拒绝(missing_tables 不让 auto-repair 跑)

无 Docker 环境(DockerNotFoundError / ImageNotFoundError)`pg_repo` fixture 自动 skip。
"""

from __future__ import annotations

import pytest

from tgmonitor.core.storage.postgres_repo import PostgresRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_introspect_schema_fresh_db_ok(pg_repo: PostgresRepository) -> None:
    """刚 init_schema 完的全新 schema 应 ok=True。"""
    report = await pg_repo.introspect_schema()
    assert report.ok is True
    assert report.missing_tables == []
    assert report.missing_columns == []
    assert report.summary() == "ok"


async def test_introspect_schema_reports_missing_column(
    pg_repo: PostgresRepository,
) -> None:
    """DROP messages.media_album_id → introspect 报 missing_columns。"""
    async with pg_repo._pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute("ALTER TABLE messages DROP COLUMN media_album_id")

    report = await pg_repo.introspect_schema()
    assert report.ok is False
    assert ("messages", "media_album_id", "BIGINT") in report.missing_columns
    assert "missing_columns" in report.summary()


async def test_introspect_schema_reports_wrong_type(
    pg_repo: PostgresRepository,
) -> None:
    """messages.views 类型从 INTEGER 改成 TEXT → introspect 报 wrong_types。"""
    async with pg_repo._pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute("ALTER TABLE messages ALTER COLUMN views TYPE TEXT")

    report = await pg_repo.introspect_schema()
    # wrong_types 不影响 ok(契约)— 但要检测到 drift
    assert report.ok is True
    drifts = {(d.table, d.column, d.expected_type, d.actual_type) for d in report.wrong_types}
    assert ("messages", "views", "INTEGER", "TEXT") in drifts


async def test_repair_schema_adds_missing_column(
    pg_repo: PostgresRepository,
) -> None:
    """DROP messages.media_album_id → repair_schema 跑 ALTER ADD COLUMN IF NOT EXISTS
    → 二次 introspect 应 ok=True。"""
    async with pg_repo._pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute("ALTER TABLE messages DROP COLUMN media_album_id")

    pre = await pg_repo.introspect_schema()
    assert ("messages", "media_album_id", "BIGINT") in pre.missing_columns

    await pg_repo.repair_schema(pre)

    post = await pg_repo.introspect_schema()
    assert post.ok is True
    assert post.missing_columns == []

    # 真实列确实回来了
    async with pg_repo._pool.acquire() as conn:  # type: ignore[attr-defined]
        has_col = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='messages' "
            "AND column_name='media_album_id'"
        )
    assert has_col == 1


async def test_repair_schema_idempotent(pg_repo: PostgresRepository) -> None:
    """repair_schema 跑两次不抛;二次 introspect 仍 ok。"""
    async with pg_repo._pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute("ALTER TABLE messages DROP COLUMN media_album_id")

    report = await pg_repo.introspect_schema()
    await pg_repo.repair_schema(report)
    # 第二次 repair 跑在 ok 报告上:no-op
    await pg_repo.repair_schema(report)
    # 第三次:再 introspect 应 ok
    final = await pg_repo.introspect_schema()
    assert final.ok is True


async def test_repair_schema_refuses_missing_table(
    pg_repo: PostgresRepository,
) -> None:
    """DROP TABLE messages → repair 抛 RuntimeError(避免 DROP+CREATE 丢数据)。"""
    async with pg_repo._pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute("DROP TABLE messages")

    report = await pg_repo.introspect_schema()
    assert "messages" in report.missing_tables
    assert report.ok is False

    with pytest.raises(RuntimeError, match="missing tables"):
        await pg_repo.repair_schema(report)


async def test_repair_schema_logs_wrong_type_without_altering(
    pg_repo: PostgresRepository,
) -> None:
    """wrong_types repair 不动(类型 ALTER 重写表对大表 lock-heavy),
    只 log warning;列类型保持 TEXT。"""
    async with pg_repo._pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute("ALTER TABLE messages ALTER COLUMN views TYPE TEXT")

    report = await pg_repo.introspect_schema()
    assert any(d.column == "views" and d.actual_type == "TEXT" for d in report.wrong_types)

    await pg_repo.repair_schema(report)

    # 列类型应仍是 TEXT(repair 不动 wrong_types)
    async with pg_repo._pool.acquire() as conn:  # type: ignore[attr-defined]
        actual = await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='messages' "
            "AND column_name='views'"
        )
    assert actual == "text"

    # 二次 introspect 仍报告 wrong_types(误报契约)
    report2 = await pg_repo.introspect_schema()
    assert any(d.column == "views" and d.actual_type == "TEXT" for d in report2.wrong_types)
