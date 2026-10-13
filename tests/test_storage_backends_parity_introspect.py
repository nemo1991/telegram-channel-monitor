"""2026-09-23 v1.8.x:启动期 schema introspect/repair 在 Mongo 后端的 parity(2026-10-12 plan-batch3 目标 3 step 3.1:Jsonl 已删,Jsonl parity 测试随本 step 一起删)。

策略:Mongo 走 mongomock_motor(in-process,无需 Docker)。
PG 真集在 `tests/integration/test_pg_repo_introspect_repair.py` 单独覆盖
(需要 testcontainers,Docker 不可用时自动 skip)。

覆盖契约:
- 后端 `introspect_schema()` 返 SchemaReport(无异常)
- 默认状态(刚 connect + init_schema)→ ok=True(summary="ok")
- `repair_schema(report)` 不抛
- 修后再 introspect 仍 ok=True(幂等)
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient

from tgmonitor.core.storage.mongo_repo import MongoRepository
from tgmonitor.core.storage.schema_report import SchemaReport

# ---- fixtures ----


@pytest_asyncio.fixture
async def mongo_repo() -> AsyncIterator[MongoRepository]:
    client = AsyncMongoMockClient()
    repo = MongoRepository.from_client(client, database="tgmonitor_test_parity_introspect")
    await repo.connect()
    await repo.init_schema()
    try:
        yield repo
    finally:
        await repo.close()


# ---- parity ----


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_introspect_mongo_ok_after_connect(mongo_repo: MongoRepository) -> None:
    """Mongo 刚 connect + init_schema 后,期望唯一索引齐全 → ok=True。"""
    report = await mongo_repo.introspect_schema()
    assert isinstance(report, SchemaReport)
    assert report.ok is True
    assert report.summary() == "ok"


@pytest.mark.asyncio
async def test_repair_mongo_idempotent(mongo_repo: MongoRepository) -> None:
    """Mongo repair 在 ok=True 报告上是 no-op;在 missing 报告上重建唯一索引。"""
    # ok=True 报告:repair 是 no-op
    ok_report = SchemaReport()
    await mongo_repo.repair_schema(ok_report)
    report_after = await mongo_repo.introspect_schema()
    assert report_after.ok is True

    # missing 报告:repair 应重建索引,mongomock_motor 会接受 create_index,
    # 然后第二次 introspect ok=True
    missing_report = SchemaReport(
        missing_columns=[("messages", "(channel_id, telegram_msg_id)", "UNIQUE INDEX")]
    )
    await mongo_repo.repair_schema(missing_report)
    report_after2 = await mongo_repo.introspect_schema()
    assert report_after2.ok is True
