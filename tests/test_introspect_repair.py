"""2026-09-23 v1.8.x:启动期 schema introspect / repair — 纯单元测试。

只覆盖 EXPECTED_SCHEMA(单源)与 SchemaReport 数据类的契约;真 PG/Mongo/JSONL
后端行为在 `test_pg_repo_introspect_repair.py`(testcontainers)与
`test_storage_backends_parity_introspect.py`(parametrize mongo+jsonl)。
"""

from __future__ import annotations

import pytest

from tgmonitor.core.storage.expected_schema import EXPECTED_SCHEMA, EXPECTED_TABLES
from tgmonitor.core.storage.schema_report import ColumnDrift, SchemaReport


def test_expected_tables_covers_all_storage_tables() -> None:
    """EXPECTED_TABLES 必须等于 {channels, messages, media, meta}。"""
    assert frozenset({"channels", "messages", "media", "meta"}) == EXPECTED_TABLES


def test_expected_schema_messages_has_v14_columns() -> None:
    """2026-08-27 v1.4.0 PR #9 + PR #10 + 2026-09-09 v1.7.2 PR #11:
    messages 表必须含 4 个 v1.4.0 PR #9 列 + reactions / is_favorite / tags / notes。
    """
    cols = EXPECTED_SCHEMA["messages"]
    assert cols["forward_origin"] == "JSONB"
    assert cols["via_bot_user_id"] == "BIGINT"
    assert cols["media_album_id"] == "BIGINT"
    assert cols["is_pinned"] == "BOOLEAN"
    assert cols["reactions"] == "JSONB"
    assert cols["is_favorite"] == "BOOLEAN"
    assert cols["tags"] == "TEXT[]"
    assert cols["notes"] == "TEXT"


def test_expected_schema_channels_has_spammer_filter_fields() -> None:
    """2026-09-04 v1.6.4:channels 4 个 spammer 过滤字段必须在。"""
    cols = EXPECTED_SCHEMA["channels"]
    for field, typ in [
        ("is_verified", "BOOLEAN"),
        ("is_scam", "BOOLEAN"),
        ("is_fake", "BOOLEAN"),
        ("has_protected_content", "BOOLEAN"),
    ]:
        assert cols[field] == typ, f"{field} 应为 {typ}"


def test_expected_schema_int53_fields_are_bigint_or_integer() -> None:
    """2026-09-23 asyncpg `'0'` 报错字段必须都是 BIGINT / INTEGER(不能 TEXT)。"""
    int_fields = {
        ("messages", "views"),
        ("messages", "forwards"),
        ("messages", "reply_to_msg_id"),
        ("messages", "via_bot_user_id"),
        ("messages", "media_album_id"),
        ("media", "file_size"),
        ("media", "width"),
        ("media", "height"),
        ("media", "duration"),
        ("channels", "member_count"),
    }
    for table, field in int_fields:
        typ = EXPECTED_SCHEMA[table][field]
        assert typ in ("BIGINT", "INTEGER"), f"{table}.{field} 类型 {typ!r} 应为 BIGINT/INTEGER"


def test_schema_report_ok_when_empty() -> None:
    r = SchemaReport()
    assert r.ok is True
    assert r.summary() == "ok"


def test_schema_report_not_ok_when_missing_columns() -> None:
    r = SchemaReport(missing_columns=[("messages", "media_album_id", "BIGINT")])
    assert r.ok is False
    assert "missing_columns=1" in r.summary()


def test_schema_report_not_ok_when_missing_tables() -> None:
    r = SchemaReport(missing_tables=["messages"])
    assert r.ok is False
    assert "missing_tables" in r.summary()


def test_schema_report_ok_when_only_wrong_types_or_extra() -> None:
    """wrong_types / extra_columns 不影响 ok(它们仍要 log,但不阻塞)。"""
    r = SchemaReport(
        wrong_types=[
            ColumnDrift("messages", "views", "INTEGER", "TEXT"),
        ],
        extra_columns=[("messages", "legacy_field", "TEXT")],
    )
    assert r.ok is True
    s = r.summary()
    assert "wrong_types=1" in s
    assert "extra_columns=1" in s


def test_schema_report_frozen() -> None:
    """SchemaReport 必须 frozen(防止 bootstrap / repair 路径意外 mutate)。"""
    r = SchemaReport()
    with pytest.raises((AttributeError, TypeError)):
        r.missing_tables = ["x"]  # type: ignore[misc]
