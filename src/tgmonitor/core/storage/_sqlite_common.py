"""SQLite 仓储共享 helper 与 schema 常量 — 2026-11-25 P2-N1。

从 `sqlite_repo.py` 抽出的模块级 schema 常量(`SCHEMA_SQL` /
`EXPECTED_SCHEMA` / `EXPECTED_TABLES`)与 12 个序列化 / row 映射 helper,
供 `SqliteRepository` 主类及后续按域拆分的 mixin 共用(避免 mixin import
主类形成循环)。纯函数 + 常量,无类依赖、无后端连接状态。

`SqliteRepository` 与 `tests/test_storage_migration_2006.py` 经 re-export
继续引用 `EXPECTED_SCHEMA` 等符号,契约不变。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from tgmonitor.core.dto import (
    ChannelDTO,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    ReactionDTO,
    ThumbnailDTO,
)
from tgmonitor.core.storage._media_codec import (
    _media_status,
    _status_to_db,
)

SCHEMA_SQL = """\
-- SQLite 参考 DDL — 与 PG schema 逻辑对照,幂等(init_schema 启动时执行)。

CREATE TABLE IF NOT EXISTS channels (
    id              INTEGER PRIMARY KEY,              -- Telegram chat_id
    title           TEXT    NOT NULL,
    username        TEXT,
    kind            TEXT    NOT NULL DEFAULT 'channel',
    member_count    INTEGER,
    created_at      TEXT,
    first_seen_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    monitored       INTEGER NOT NULL DEFAULT 0,   -- 语义=是否监听(白名单);物理列 2.0.6 由 subscribed 正名
    tg_subscribed   INTEGER NOT NULL DEFAULT 0,   -- v2.0.3:TG 侧是否仍订阅/加入(与监听独立)
    last_synced_at  TEXT,
    photo_local_key TEXT,
    is_verified     INTEGER NOT NULL DEFAULT 0,
    is_scam         INTEGER NOT NULL DEFAULT 0,
    is_fake         INTEGER NOT NULL DEFAULT 0,
    has_protected_content INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    telegram_msg_id     INTEGER NOT NULL,
    author              TEXT,
    date                TEXT    NOT NULL,
    text                TEXT    NOT NULL DEFAULT '',
    views               INTEGER,
    forwards            INTEGER,
    reply_to_msg_id     INTEGER,
    edited              INTEGER NOT NULL DEFAULT 0,
    raw                 TEXT,
    forward_origin      TEXT,
    via_bot_user_id     INTEGER,
    media_album_id      INTEGER,
    is_pinned           INTEGER NOT NULL DEFAULT 0,
    reactions           TEXT,
    is_favorite         INTEGER NOT NULL DEFAULT 0,
    tags                TEXT    NOT NULL DEFAULT '[]',
    notes               TEXT    NOT NULL DEFAULT '',
    UNIQUE (channel_id, telegram_msg_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_channel_date
    ON messages (channel_id, date);

CREATE INDEX IF NOT EXISTS idx_messages_date
    ON messages (date);

CREATE TABLE IF NOT EXISTS media (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id          INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    type                TEXT    NOT NULL,
    mime_type           TEXT,
    file_name           TEXT,
    file_size           INTEGER,
    width               INTEGER,
    height              INTEGER,
    duration            INTEGER,
    telegram_file_id    TEXT,
    object_key          TEXT,
    object_backend      TEXT,
    emoji               TEXT,
    download_status     TEXT    NOT NULL DEFAULT 'pending',
    download_error      TEXT,
    thumbnail_telegram_file_id TEXT
);

-- 历史数据迁移:已有 object_key 的行视为已下载(done);与 PG 对齐。
UPDATE media SET download_status = 'done'
    WHERE object_key IS NOT NULL AND download_status = 'pending';

CREATE INDEX IF NOT EXISTS idx_media_message
    ON media (message_id);

-- 跨消息媒体去重(find_media_by_file_id):部分索引只收已下载行。
CREATE INDEX IF NOT EXISTS idx_media_telegram_file_id
    ON media (telegram_file_id) WHERE object_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

-- 缩略图独立表:关联键 (channel_id, telegram_msg_id, media_idx) 三元组,
-- FK 走 messages 的 UNIQUE (channel_id, telegram_msg_id),消息删时级联。
CREATE TABLE IF NOT EXISTS thumbnails (
    channel_id               INTEGER NOT NULL,
    telegram_msg_id          INTEGER NOT NULL,
    media_idx                INTEGER NOT NULL,
    object_key               TEXT,
    object_backend           TEXT,
    mime_type                TEXT,
    file_size                INTEGER,
    width                    INTEGER,
    height                   INTEGER,
    sha256                   TEXT,
    download_status          TEXT    NOT NULL DEFAULT 'pending',
    download_error           TEXT,
    telegram_thumb_file_id   TEXT,
    PRIMARY KEY (channel_id, telegram_msg_id, media_idx),
    FOREIGN KEY (channel_id, telegram_msg_id)
        REFERENCES messages (channel_id, telegram_msg_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_thumbnails_object_key
    ON thumbnails (object_key) WHERE object_key IS NOT NULL;

-- v2.0.4 FR-05:运维活动记录(事件时间线持久化)。全留存,不裁剪。
CREATE TABLE IF NOT EXISTS activities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type          TEXT    NOT NULL,
    occurred_at         TEXT    NOT NULL,
    icon                TEXT    NOT NULL DEFAULT '',
    title               TEXT    NOT NULL DEFAULT '',
    detail              TEXT    NOT NULL DEFAULT '',
    severity            TEXT    NOT NULL DEFAULT 'info',
    channel_id          INTEGER,
    telegram_msg_id     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_activities_occurred
    ON activities (occurred_at DESC, id DESC);
"""


# introspect_schema 对照源 — 与 SCHEMA_SQL 声明类型同步维护
# (tests/test_sqlite_repo.py::test_expected_schema_matches_ddl 守门)。
EXPECTED_SCHEMA: dict[str, dict[str, str]] = {
    "channels": {
        "id": "INTEGER",
        "title": "TEXT",
        "username": "TEXT",
        "kind": "TEXT",
        "member_count": "INTEGER",
        "created_at": "TEXT",
        "first_seen_at": "TEXT",
        "monitored": "INTEGER",
        "tg_subscribed": "INTEGER",
        "last_synced_at": "TEXT",
        "photo_local_key": "TEXT",
        "is_verified": "INTEGER",
        "is_scam": "INTEGER",
        "is_fake": "INTEGER",
        "has_protected_content": "INTEGER",
    },
    "messages": {
        "id": "INTEGER",
        "channel_id": "INTEGER",
        "telegram_msg_id": "INTEGER",
        "author": "TEXT",
        "date": "TEXT",
        "text": "TEXT",
        "views": "INTEGER",
        "forwards": "INTEGER",
        "reply_to_msg_id": "INTEGER",
        "edited": "INTEGER",
        "raw": "TEXT",
        "forward_origin": "TEXT",
        "via_bot_user_id": "INTEGER",
        "media_album_id": "INTEGER",
        "is_pinned": "INTEGER",
        "reactions": "TEXT",
        "is_favorite": "INTEGER",
        "tags": "TEXT",
        "notes": "TEXT",
    },
    "media": {
        "id": "INTEGER",
        "message_id": "INTEGER",
        "type": "TEXT",
        "mime_type": "TEXT",
        "file_name": "TEXT",
        "file_size": "INTEGER",
        "width": "INTEGER",
        "height": "INTEGER",
        "duration": "INTEGER",
        "telegram_file_id": "TEXT",
        "object_key": "TEXT",
        "object_backend": "TEXT",
        "emoji": "TEXT",
        "download_status": "TEXT",
        "download_error": "TEXT",
        "thumbnail_telegram_file_id": "TEXT",
    },
    "meta": {
        "key": "TEXT",
        "value": "TEXT",
    },
    "thumbnails": {
        "channel_id": "INTEGER",
        "telegram_msg_id": "INTEGER",
        "media_idx": "INTEGER",
        "object_key": "TEXT",
        "object_backend": "TEXT",
        "mime_type": "TEXT",
        "file_size": "INTEGER",
        "width": "INTEGER",
        "height": "INTEGER",
        "sha256": "TEXT",
        "download_status": "TEXT",
        "download_error": "TEXT",
        "telegram_thumb_file_id": "TEXT",
    },
    "activities": {
        "id": "INTEGER",
        "event_type": "TEXT",
        "occurred_at": "TEXT",
        "icon": "TEXT",
        "title": "TEXT",
        "detail": "TEXT",
        "severity": "TEXT",
        "channel_id": "INTEGER",
        "telegram_msg_id": "INTEGER",
    },
}

EXPECTED_TABLES: frozenset[str] = frozenset(EXPECTED_SCHEMA.keys())

# ---- 序列化 helper ----
# `_MEDIA_SORT_COLUMN` / `_media_status` / `_status_to_db` 已收口到
# `_media_codec.py`(2026-11-25 P1-N2),由顶部 import 引入。


def _dt_to_db(dt: datetime | None) -> str | None:
    """aware → UTC ISO;naive → 原样 ISO(读回时各自还原,round-trip == 成立)。"""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.isoformat()


def _dt_from_db(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _json_to_db(value: Any) -> str | None:
    return json.dumps(value) if value is not None else None


def _json_from_db(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


def _sql_lower(value: str | None) -> str | None:
    """注册到 SQLite 的 LOWER — Python str.lower 是 Unicode 感知,对齐 PG。"""
    return value.lower() if value is not None else None


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


# ---- row → DTO ----


def _row_to_channel(row: dict[str, Any]) -> ChannelDTO:
    return ChannelDTO(
        id=row["id"],
        title=row["title"],
        username=row["username"],
        kind=row["kind"],
        member_count=row["member_count"],
        created_at=_dt_from_db(row["created_at"]),
        # 物理列 monitored 承载"监听"语义(2.0.6 由 subscribed 正名)。
        is_monitored=bool(row.get("monitored", False)),
        # v2.0.3:TG 订阅状态;旧库无此列时默认 False(不回填,靠刷新校正)。
        is_tg_subscribed=bool(row.get("tg_subscribed", False)),
        last_synced_at=_dt_from_db(row.get("last_synced_at")),
        photo_local_key=row.get("photo_local_key"),
        is_verified=bool(row.get("is_verified", False)),
        is_scam=bool(row.get("is_scam", False)),
        is_fake=bool(row.get("is_fake", False)),
        has_protected_content=bool(row.get("has_protected_content", False)),
    )


def _row_to_media(row: dict[str, Any]) -> MediaDTO:
    return MediaDTO(
        type=MediaType(row["type"]),
        mime_type=row["mime_type"],
        file_name=row["file_name"],
        file_size=row["file_size"],
        width=row["width"],
        height=row["height"],
        duration=row["duration"],
        telegram_file_id=row["telegram_file_id"],
        thumbnail_telegram_file_id=row.get("thumbnail_telegram_file_id"),
        object_key=row["object_key"],
        object_backend=row["object_backend"],
        emoji=row["emoji"],
        download_status=_media_status(row.get("download_status")),
        download_error=row.get("download_error"),
    )


def _row_to_message(row: dict[str, Any], media: list[MediaDTO]) -> MessageDTO:
    reactions_raw = row.get("reactions")
    reactions: list[ReactionDTO] | None
    if reactions_raw is None:
        reactions = None
    else:
        reactions = [ReactionDTO.from_dict(d) for d in _json_from_db(reactions_raw)]
    date = _dt_from_db(row["date"])
    assert date is not None  # date 列 NOT NULL
    return MessageDTO(
        id=row["id"],
        channel_id=row["channel_id"],
        telegram_msg_id=row["telegram_msg_id"],
        author=row["author"],
        date=date,
        text=row["text"] or "",
        views=row["views"],
        forwards=row["forwards"],
        reply_to_msg_id=row["reply_to_msg_id"],
        edited=bool(row["edited"]),
        media=media,
        raw=_json_from_db(row["raw"]),
        forward_origin=_json_from_db(row.get("forward_origin")),
        via_bot_user_id=row.get("via_bot_user_id"),
        media_album_id=row.get("media_album_id"),
        is_pinned=bool(row.get("is_pinned", False)),
        reactions=reactions,
        is_favorite=bool(row.get("is_favorite", False)),
        tags=list(_json_from_db(row.get("tags")) or []),
        notes=row.get("notes") or "",
    )


def _row_to_thumbnail(row: dict[str, Any]) -> ThumbnailDTO:
    return ThumbnailDTO(
        channel_id=row["channel_id"],
        telegram_msg_id=row["telegram_msg_id"],
        media_idx=row["media_idx"],
        object_key=row["object_key"],
        object_backend=row["object_backend"],
        mime_type=row["mime_type"],
        file_size=row["file_size"],
        width=row["width"],
        height=row["height"],
        sha256=row["sha256"],
        download_status=_media_status(row.get("download_status")),
        download_error=row.get("download_error"),
        telegram_thumb_file_id=row.get("telegram_thumb_file_id"),
    )


def _media_to_row(message_pk: int, m: MediaDTO) -> tuple[Any, ...]:
    return (
        message_pk,
        m.type.value,
        m.mime_type,
        m.file_name,
        m.file_size,
        m.width,
        m.height,
        m.duration,
        m.telegram_file_id,
        m.thumbnail_telegram_file_id,
        m.object_key,
        m.object_backend,
        m.emoji,
        _status_to_db(m.download_status),
        m.download_error,
    )


def _media_where_clause(
    channel_ids: list[int] | None,
    status: MediaDownloadStatus | None,
    media_type: MediaType | None,
    search: str,
    undownloaded: bool = False,
) -> tuple[str, list[Any]]:
    """list_media / count_media 共用 WHERE 拼装(与 PG 同款,? 占位)。"""
    clauses: list[str] = ["1=1"]
    params: list[Any] = []
    if channel_ids:
        clauses.append(f"m.channel_id IN ({_placeholders(len(channel_ids))})")
        params.extend(channel_ids)
    if undownloaded:
        # 2026-11-22:未下载在 DB 物理值仍是 'pending'(枚举已删该值)。
        clauses.append("me.download_status = 'pending'")
    elif status is not None:
        clauses.append("me.download_status = ?")
        params.append(status.value)
    if media_type is not None:
        clauses.append("me.type = ?")
        params.append(media_type.value)
    if search:
        clauses.append("LOWER(COALESCE(me.file_name, '')) LIKE ?")
        params.append(f"%{search.lower()}%")
    return " AND ".join(clauses), params


