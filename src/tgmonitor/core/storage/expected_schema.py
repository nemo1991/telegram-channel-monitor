"""期望 Postgres schema — 与 `schema.sql` 同步维护的单一对照源。

2026-09-23 v1.8.x:启动期 introspect/auto-repair 机制引入。
`introspect_schema()` 拿这个 dict 与 `information_schema.columns` 比对,
drift 自动 ADD COLUMN(无锁 / 幂等);wrong_type 不动(类型 ALTER 重写表
对大表 lock-heavy,运维 warning 后手改)。

类型字符串匹配 `information_schema.columns.data_type` 大写
(`BIGINT` / `INTEGER` / `TEXT` / `BOOLEAN` / `TIMESTAMPTZ` / `JSONB`)。
**BIGSERIAL 视作 BIGINT**(PG 把 BIGSERIAL 当 BIGINT + identity)。

跟 `schema.sql` 一起改,以后任何 ALTER TABLE 都改这两处;测试
`test_introspect_repair.py::test_expected_schema_matches_pg_sql_columns`
锁两文件对齐。
"""

from __future__ import annotations

EXPECTED_SCHEMA: dict[str, dict[str, str]] = {
    "channels": {
        "id": "BIGINT",
        "title": "TEXT",
        "username": "TEXT",
        "kind": "TEXT",
        "member_count": "INTEGER",
        "created_at": "TIMESTAMPTZ",
        "first_seen_at": "TIMESTAMPTZ",
        "subscribed": "BOOLEAN",
        "last_synced_at": "TIMESTAMPTZ",
        "photo_local_key": "TEXT",
        "is_verified": "BOOLEAN",
        "is_scam": "BOOLEAN",
        "is_fake": "BOOLEAN",
        "has_protected_content": "BOOLEAN",
    },
    "messages": {
        "id": "BIGSERIAL",
        "channel_id": "BIGINT",
        "telegram_msg_id": "BIGINT",
        "author": "TEXT",
        "date": "TIMESTAMPTZ",
        "text": "TEXT",
        "views": "INTEGER",
        "forwards": "INTEGER",
        "reply_to_msg_id": "BIGINT",
        "edited": "BOOLEAN",
        "raw": "JSONB",
        "forward_origin": "JSONB",
        "via_bot_user_id": "BIGINT",
        "media_album_id": "BIGINT",
        "is_pinned": "BOOLEAN",
        "reactions": "JSONB",
        "is_favorite": "BOOLEAN",
        "tags": "TEXT[]",
        "notes": "TEXT",
    },
    "media": {
        "id": "BIGSERIAL",
        "message_id": "BIGINT",
        "type": "TEXT",
        "mime_type": "TEXT",
        "file_name": "TEXT",
        "file_size": "BIGINT",
        "width": "INTEGER",
        "height": "INTEGER",
        "duration": "INTEGER",
        "telegram_file_id": "TEXT",
        "object_key": "TEXT",
        "object_backend": "TEXT",
        "thumb_key": "TEXT",
        "thumb_backend": "TEXT",
        "emoji": "TEXT",
        "download_status": "TEXT",
        "download_error": "TEXT",
    },
    "meta": {
        "key": "TEXT",
        "value": "TEXT",
    },
}

EXPECTED_TABLES: frozenset[str] = frozenset(EXPECTED_SCHEMA.keys())


__all__ = ["EXPECTED_SCHEMA", "EXPECTED_TABLES"]
