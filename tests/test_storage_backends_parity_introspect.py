"""2026-09-23 v1.8.x:启动期 schema introspect/repair 在 Mongo + JSONL 两后端的 parity。

策略:Mongo 走 mongomock_motor(in-process,无需 Docker);JSONL 走 tmp_path。
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
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient

from tgmonitor.core.dto import ChannelDTO, MediaType
from tgmonitor.core.storage.jsonl_store import JsonlFileStore
from tgmonitor.core.storage.mongo_repo import MongoRepository
from tgmonitor.core.storage.schema_report import SchemaReport

# ---- fixtures ----


@pytest_asyncio.fixture
async def jsonl_repo(tmp_path) -> AsyncIterator[JsonlFileStore]:
    repo = JsonlFileStore(root=tmp_path / "jsonl")
    await repo.connect()
    await repo.init_schema()
    # 订阅一个频道,让 _files dict 有内容(introspect 才会检查所有 message rows)
    await repo.upsert_channel(ChannelDTO(id=1, title="#1"))
    await repo.set_channel_subscribed(1, True)
    try:
        yield repo
    finally:
        await repo.close()


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
async def test_introspect_jsonl_ok_after_connect(jsonl_repo: JsonlFileStore) -> None:
    """JSONL 空仓库 introspect 应 ok(无 message rows → 无污染)。"""
    report = await jsonl_repo.introspect_schema()
    assert isinstance(report, SchemaReport)
    assert report.ok is True
    assert report.summary() == "ok"


@pytest.mark.asyncio
async def test_introspect_jsonl_detects_str_pollution(jsonl_repo: JsonlFileStore) -> None:
    """JSONL introspect 检测 message 字段被存成 str(典型 '0' 污染)。

    直接 mutate ChannelFile.rows(模拟旧库 '0' 字符串污染),
    expected 走 introspect_schema 检出。
    """
    from tgmonitor.core.storage.channel_file import ChannelFile

    cf = ChannelFile(jsonl_repo._msg_dir / "1.jsonl")
    await cf.load()
    cf.rows.append(
        {
            "id": 1,
            "channel_id": 1,
            "telegram_msg_id": 1,
            "author": None,
            "date": datetime.now(UTC).isoformat(),
            "text": "polluted",
            "views": "0",  # ← str pollution
            "forwards": "0",
            "reply_to_msg_id": "0",
            "via_bot_user_id": "0",
            "media_album_id": "0",
            "is_pinned": False,
            "edited": False,
            "reactions": None,
            "is_favorite": False,
            "tags": [],
            "notes": "",
            "media": [
                {
                    "type": MediaType.PHOTO.value,
                    "mime_type": "image/jpeg",
                    "file_name": "x.jpg",
                    "file_size": "0",  # ← str pollution in media
                    "width": "100",
                    "height": "100",
                    "duration": "0",
                    "telegram_file_id": "f1",
                    "object_key": None,
                    "object_backend": None,
                    "thumb_key": None,
                    "thumb_backend": None,
                    "emoji": None,
                    "download_status": "pending",
                    "download_error": None,
                }
            ],
        }
    )
    jsonl_repo._files[1] = cf

    report = await jsonl_repo.introspect_schema()
    # wrong_types 不影响 ok(契约:`ok = not missing_tables and not missing_columns`)
    # 这里验证 wrong_types 数量 + 字段名是否含被污染字段
    assert report.ok is True
    bad_fields = {(d.table, d.column) for d in report.wrong_types}
    # messages 表污染字段
    assert ("messages", "views") in bad_fields
    assert ("messages", "media_album_id") in bad_fields
    # media 表污染字段
    assert ("media", "file_size") in bad_fields


@pytest.mark.asyncio
async def test_repair_jsonl_logs_only(jsonl_repo: JsonlFileStore) -> None:
    """JSONL repair 只 log warning,不动磁盘(自动改文件风险大)。"""
    report = SchemaReport(
        wrong_types=[],
    )
    # repair on empty report:无 log,无异常
    await jsonl_repo.repair_schema(report)


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
