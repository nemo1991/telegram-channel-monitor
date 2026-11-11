"""SQLite 仓储 — v2.0.0 默认后端(aiosqlite,WAL)。

单文件内聚实现 `StorageRepository` 全部 42 方法,查询语义与
`postgres_repo` 逐条对齐(契约测试 `tests/test_storage_backends.py`
parametrize 守门)。

设计要点:
- 单连接 + `asyncio.Lock` 串行化所有操作。SQLite 单写者,桌面规模下
  串行无性能问题;锁同时避免「同连接读到未提交事务中间态」。
- `isolation_level=None`(autocommit);`save_message` 显式 BEGIN/COMMIT。
- WAL + `foreign_keys=ON` + `busy_timeout=5000` + `synchronous=NORMAL`。
- datetime 序列化:aware → UTC ISO 字符串,naive → 原样 ISO;读回
  `datetime.fromisoformat`(naive/aware 各自还原,round-trip `==` 成立)。
  排序按 ISO 字符串字典序 — 全 naive(测试)或全 UTC(生产)时与 PG
  timestamptz 语义一致;naive/aware 混存的极端场景不做保证。
- `tags` 用 JSON 数组文本(JSON1 `json_each` / `json_array_length` 查询),
  `raw` / `forward_origin` / `reactions` 用 JSON 文本。
- `LOWER` 注册为 Python `str.lower`(Unicode 感知),对齐 PG 的
  Unicode LOWER(SQLite 内置 LOWER 只折 ASCII)。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from tgmonitor.core.dto import (
    ChannelDTO,
    ChannelStats,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    ReactionDTO,
    SortDir,
    SortKey,
    ThumbnailDTO,
)
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.storage.schema_report import ColumnDrift, SchemaReport

log = logging.getLogger(__name__)


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
    subscribed      INTEGER NOT NULL DEFAULT 0,
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
        "subscribed": "INTEGER",
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
}

EXPECTED_TABLES: frozenset[str] = frozenset(EXPECTED_SCHEMA.keys())

# SortKey → ORDER BY 列(SIZE 可能 NULL → NULLS LAST,与 PG 对齐)。
_MEDIA_SORT_COLUMN: dict[SortKey, tuple[str, bool]] = {
    SortKey.DATE: ("m.date", False),
    SortKey.SIZE: ("me.file_size", True),
    SortKey.STATUS: ("me.download_status", False),
}


# ---- 序列化 helper ----


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


def _media_status(value: object | None) -> MediaDownloadStatus:
    """反序列化 download_status;非法 / 缺失回退 pending(与 PG 对齐)。"""
    try:
        return MediaDownloadStatus(str(value))
    except ValueError:
        return MediaDownloadStatus.PENDING


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
        is_subscribed=bool(row.get("subscribed", True)),
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
        m.download_status.value,
        m.download_error,
    )


def _media_where_clause(
    channel_ids: list[int] | None,
    status: MediaDownloadStatus | None,
    media_type: MediaType | None,
    search: str,
) -> tuple[str, list[Any]]:
    """list_media / count_media 共用 WHERE 拼装(与 PG 同款,? 占位)。"""
    clauses: list[str] = ["1=1"]
    params: list[Any] = []
    if channel_ids:
        clauses.append(f"m.channel_id IN ({_placeholders(len(channel_ids))})")
        params.extend(channel_ids)
    if status is not None:
        clauses.append("me.download_status = ?")
        params.append(status.value)
    if media_type is not None:
        clauses.append("me.type = ?")
        params.append(media_type.value)
    if search:
        clauses.append("LOWER(COALESCE(me.file_name, '')) LIKE ?")
        params.append(f"%{search.lower()}%")
    return " AND ".join(clauses), params


class SqliteRepository(StorageRepository):
    """SQLite 后端 — 单连接 + asyncio.Lock 串行化;WAL 模式。"""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path) if path != ":memory:" else path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ---- 生命周期 ----

    async def connect(self) -> None:
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

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def init_schema(self) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.executescript(SCHEMA_SQL)

    async def introspect_schema(self) -> SchemaReport:
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

    async def repair_schema(self, report: SchemaReport) -> None:
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

    async def ping(self) -> bool:
        if self._db is None:
            return False
        try:
            async with self._lock:
                await self._db.execute("SELECT 1")
            return True
        except Exception:
            return False

    # ---- 频道 ----

    async def upsert_channel(self, channel: ChannelDTO) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                """
                INSERT INTO channels
                    (id, title, username, kind, member_count, created_at,
                     subscribed, last_synced_at, photo_local_key,
                     is_verified, is_scam, is_fake, has_protected_content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    title = excluded.title,
                    username = excluded.username,
                    kind = excluded.kind,
                    member_count = excluded.member_count,
                    created_at = excluded.created_at,
                    subscribed = excluded.subscribed,
                    last_synced_at = excluded.last_synced_at,
                    photo_local_key = excluded.photo_local_key,
                    is_verified = excluded.is_verified,
                    is_scam = excluded.is_scam,
                    is_fake = excluded.is_fake,
                    has_protected_content = excluded.has_protected_content
                """,
                (
                    channel.id,
                    channel.title,
                    channel.username,
                    channel.kind,
                    channel.member_count,
                    _dt_to_db(channel.created_at),
                    channel.is_subscribed,
                    _dt_to_db(channel.last_synced_at),
                    channel.photo_local_key,
                    channel.is_verified,
                    channel.is_scam,
                    channel.is_fake,
                    channel.has_protected_content,
                ),
            )

    async def upsert_channel_metadata(self, channel: ChannelDTO) -> None:
        """只更元数据;subscribed 保持旧值(首次插入用 caller 值)。"""
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                """
                INSERT INTO channels
                    (id, title, username, kind, member_count, created_at,
                     subscribed, last_synced_at, photo_local_key,
                     is_verified, is_scam, is_fake, has_protected_content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    title = excluded.title,
                    username = excluded.username,
                    kind = excluded.kind,
                    member_count = excluded.member_count,
                    created_at = excluded.created_at,
                    last_synced_at = excluded.last_synced_at,
                    photo_local_key = excluded.photo_local_key,
                    is_verified = excluded.is_verified,
                    is_scam = excluded.is_scam,
                    is_fake = excluded.is_fake,
                    has_protected_content = excluded.has_protected_content
                """,
                (
                    channel.id,
                    channel.title,
                    channel.username,
                    channel.kind,
                    channel.member_count,
                    _dt_to_db(channel.created_at),
                    channel.is_subscribed,
                    _dt_to_db(channel.last_synced_at),
                    channel.photo_local_key,
                    channel.is_verified,
                    channel.is_scam,
                    channel.is_fake,
                    channel.has_protected_content,
                ),
            )

    async def update_channel_metadata(
        self,
        channel_id: int,
        *,
        title: str | None = None,
        username: str | None = None,
        member_count: int | None = None,
        photo_local_key: str | None = None,
        is_verified: bool | None = None,
        is_scam: bool | None = None,
        is_fake: bool | None = None,
        has_protected_content: bool | None = None,
    ) -> None:
        """部分更新 — COALESCE(?, 列) 把 None 映射成「保留旧值」。"""
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                """
                UPDATE channels SET
                    title                 = COALESCE(?, title),
                    username              = COALESCE(?, username),
                    member_count          = COALESCE(?, member_count),
                    photo_local_key       = COALESCE(?, photo_local_key),
                    is_verified           = COALESCE(?, is_verified),
                    is_scam               = COALESCE(?, is_scam),
                    is_fake               = COALESCE(?, is_fake),
                    has_protected_content = COALESCE(?, has_protected_content)
                WHERE id = ?
                """,
                (
                    title,
                    username,
                    member_count,
                    photo_local_key,
                    is_verified,
                    is_scam,
                    is_fake,
                    has_protected_content,
                    channel_id,
                ),
            )

    async def set_channel_subscribed(self, channel_id: int, subscribed: bool) -> None:
        """只设订阅标志;频道未建档时用 id 做 stub。"""
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                """
                INSERT INTO channels (id, title, subscribed)
                VALUES (?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET subscribed = excluded.subscribed
                """,
                (channel_id, f"#{channel_id}", subscribed),
            )

    async def list_channels(self) -> list[ChannelDTO]:
        assert self._db is not None
        async with self._lock:
            rows = await self._db.execute_fetchall("SELECT * FROM channels ORDER BY id")
        return [_row_to_channel(dict(r)) for r in rows]

    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        assert self._db is not None
        async with self._lock:
            rows = await self._db.execute_fetchall(
                "SELECT * FROM channels WHERE subscribed = 1 ORDER BY id"
            )
        return [_row_to_channel(dict(r)) for r in rows]

    async def get_channel(self, channel_id: int) -> ChannelDTO | None:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
            row = await cur.fetchone()
        return _row_to_channel(dict(row)) if row else None

    async def delete_channel(self, channel_id: int) -> None:
        """删频道;FK CASCADE 自动带 messages / media / thumbnails。"""
        assert self._db is not None
        async with self._lock:
            await self._db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))

    async def get_max_telegram_msg_id(self, channel_id: int) -> int | None:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT MAX(telegram_msg_id) FROM messages WHERE channel_id = ?",
                (channel_id,),
            )
            row = await cur.fetchone()
        assert row is not None  # MAX 聚合恒返一行(值可为 NULL)
        return row[0]

    # ---- 全局元数据 ----

    async def get_meta(self, key: str) -> str | None:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("SELECT value FROM meta WHERE key = ?", (key,))
            row = await cur.fetchone()
        return row[0] if row is not None else None

    async def set_meta(self, key: str, value: str) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                """
                INSERT INTO meta (key, value) VALUES (?, ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    # ---- 消息 ----

    async def save_message(self, message: MessageDTO) -> int:
        """幂等 upsert;隐式建频道占位(与 PG / InMemory 对齐);media 先清后插。

        全程单事务(BEGIN/COMMIT),异常回滚 — 不留「消息在、media 丢」中间态。
        """
        assert self._db is not None
        async with self._lock:
            await self._db.execute("BEGIN")
            try:
                await self._db.execute(
                    """
                    INSERT INTO channels (id, title, subscribed)
                    VALUES (?, ?, 0)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (message.channel_id, f"#{message.channel_id}"),
                )
                cur = await self._db.execute(
                    """
                    INSERT INTO messages
                        (channel_id, telegram_msg_id, author, date, text,
                         views, forwards, reply_to_msg_id, edited, raw,
                         forward_origin, via_bot_user_id, media_album_id, is_pinned,
                         reactions, is_favorite, tags, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (channel_id, telegram_msg_id) DO UPDATE SET
                        author = excluded.author,
                        date = excluded.date,
                        text = excluded.text,
                        views = excluded.views,
                        forwards = excluded.forwards,
                        reply_to_msg_id = excluded.reply_to_msg_id,
                        edited = excluded.edited,
                        raw = excluded.raw,
                        forward_origin = excluded.forward_origin,
                        via_bot_user_id = excluded.via_bot_user_id,
                        media_album_id = excluded.media_album_id,
                        is_pinned = excluded.is_pinned,
                        reactions = excluded.reactions,
                        is_favorite = excluded.is_favorite,
                        tags = excluded.tags,
                        notes = excluded.notes
                    RETURNING id
                    """,
                    (
                        message.channel_id,
                        message.telegram_msg_id,
                        message.author,
                        _dt_to_db(message.date),
                        message.text,
                        message.views,
                        message.forwards,
                        message.reply_to_msg_id,
                        message.edited,
                        _json_to_db(message.raw),
                        _json_to_db(message.forward_origin),
                        message.via_bot_user_id,
                        message.media_album_id,
                        message.is_pinned,
                        _json_to_db(
                            [r.to_dict() for r in message.reactions]
                            if message.reactions is not None
                            else None
                        ),
                        message.is_favorite,
                        json.dumps(message.tags),
                        message.notes,
                    ),
                )
                row = await cur.fetchone()
                assert row is not None
                msg_pk = row["id"]
                await self._db.execute("DELETE FROM media WHERE message_id = ?", (msg_pk,))
                for m in message.media:
                    await self._db.execute(
                        """
                        INSERT INTO media
                            (message_id, type, mime_type, file_name, file_size,
                             width, height, duration, telegram_file_id,
                             thumbnail_telegram_file_id,
                             object_key, object_backend,
                             emoji, download_status, download_error)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        _media_to_row(msg_pk, m),
                    )
                await self._db.commit()
            except BaseException:
                await self._db.rollback()
                raise
        message.id = msg_pk
        return msg_pk

    async def update_message(self, message: MessageDTO) -> None:
        await self.save_message(message)

    async def update_message_interactions(
        self,
        channel_id: int,
        telegram_msg_id: int,
        *,
        views: int | None = None,
        reactions: list[Any] | None = None,
    ) -> None:
        """增量更新 views / reactions;None=不动,[]=清空;不存在不抛。"""
        assert self._db is not None
        set_parts: list[str] = []
        args: list[Any] = []
        if views is not None:
            set_parts.append("views = ?")
            args.append(views)
        if reactions is not None:
            set_parts.append("reactions = ?")
            args.append(
                json.dumps([r.to_dict() if isinstance(r, ReactionDTO) else r for r in reactions])
            )
        if not set_parts:
            return
        args.extend([channel_id, telegram_msg_id])
        async with self._lock:
            await self._db.execute(
                f"UPDATE messages SET {', '.join(set_parts)} "
                "WHERE channel_id = ? AND telegram_msg_id = ?",
                args,
            )

    async def update_message_pin(
        self,
        channel_id: int,
        telegram_msg_id: int,
        is_pinned: bool,
    ) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "UPDATE messages SET is_pinned = ? WHERE channel_id = ? AND telegram_msg_id = ?",
                (is_pinned, channel_id, telegram_msg_id),
            )

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """删单条消息;media / thumbnails 走 FK CASCADE。"""
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "DELETE FROM messages WHERE channel_id = ? AND telegram_msg_id = ?",
                (channel_id, telegram_msg_id),
            )

    async def delete_messages(self, channel_id: int, msg_ids: list[int]) -> None:
        if not msg_ids:
            return
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                f"DELETE FROM messages WHERE channel_id = ? "
                f"AND telegram_msg_id IN ({_placeholders(len(msg_ids))})",
                [channel_id, *msg_ids],
            )

    async def get_message(self, channel_id: int, telegram_msg_id: int) -> MessageDTO | None:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM messages WHERE channel_id = ? AND telegram_msg_id = ?",
                (channel_id, telegram_msg_id),
            )
            row = await cur.fetchone()
            if not row:
                return None
            media_rows = await self._db.execute_fetchall(
                "SELECT * FROM media WHERE message_id = ? ORDER BY id", (row["id"],)
            )
        return _row_to_message(dict(row), [_row_to_media(dict(m)) for m in media_rows])

    async def list_messages(
        self,
        channel_ids: list[int],
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        search: str = "",
        favorite_only: bool = False,
        tag_only: bool = False,
        pinned_only: bool = False,
    ) -> list[MessageDTO]:
        """与 PG 逐条对齐:limit=最近 N 条(倒序取再反转);offset 仅在 limit 给定
        时生效(从尾部跳过);search 转义 LIKE 通配符,text OR media.file_name。"""
        assert self._db is not None
        if not channel_ids:
            return []
        where: list[str] = [f"channel_id IN ({_placeholders(len(channel_ids))})"]
        params: list[Any] = list(channel_ids)
        if date_from is not None:
            where.append("date >= ?")
            params.append(_dt_to_db(date_from))
        if date_to is not None:
            where.append("date <= ?")
            params.append(_dt_to_db(date_to))
        if favorite_only:
            where.append("is_favorite = 1")
        if tag_only:
            where.append("json_array_length(tags) > 0")
        if pinned_only:
            where.append("is_pinned = 1")
        if search:
            escaped = search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            where.append(
                "(LOWER(text) LIKE ? ESCAPE '\\' "
                "OR EXISTS (SELECT 1 FROM media WHERE media.message_id = messages.id "
                "AND LOWER(media.file_name) LIKE ? ESCAPE '\\'))"
            )
            params.extend([like, like])
        order_by = "date DESC, id DESC" if limit is not None else "date ASC, id ASC"
        sql = "SELECT * FROM messages WHERE " + " AND ".join(where) + f" ORDER BY {order_by}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
            if offset > 0:
                sql += " OFFSET ?"
                params.append(offset)
        async with self._lock:
            rows = [dict(r) for r in await self._db.execute_fetchall(sql, params)]
            if limit is not None:
                rows.reverse()
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            media_rows = await self._db.execute_fetchall(
                f"SELECT * FROM media WHERE message_id IN ({_placeholders(len(ids))}) ORDER BY id",
                ids,
            )
        by_msg: dict[int, list[MediaDTO]] = {}
        for mr in media_rows:
            by_msg.setdefault(mr["message_id"], []).append(_row_to_media(dict(mr)))
        return [_row_to_message(r, by_msg.get(r["id"], [])) for r in rows]

    async def count_messages(self, channel_id: int) -> int:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT count(*) FROM messages WHERE channel_id = ?", (channel_id,)
            )
            row = await cur.fetchone()
        assert row is not None
        return row[0]

    async def aggregate_per_channel(self, channel_ids: list[int]) -> dict[int, ChannelStats]:
        """单 GROUP BY 查询;done_media 用 SUM(CASE)(SQLite 无 FILTER 子句)。"""
        if not channel_ids:
            return {}
        assert self._db is not None
        async with self._lock:
            rows = await self._db.execute_fetchall(
                f"""
                SELECT m.channel_id,
                       count(DISTINCT m.id) AS messages,
                       count(me.id) AS media,
                       sum(CASE WHEN me.download_status = 'done' THEN 1 ELSE 0 END)
                           AS done_media,
                       max(m.date) AS last_date
                FROM messages m
                LEFT JOIN media me ON me.message_id = m.id
                WHERE m.channel_id IN ({_placeholders(len(channel_ids))})
                GROUP BY m.channel_id
                """,
                channel_ids,
            )
        out: dict[int, ChannelStats] = {}
        for r in rows:
            out[int(r["channel_id"])] = ChannelStats(
                messages=int(r["messages"]),
                media=int(r["media"]),
                done_media=int(r["done_media"]),
                last_date=_dt_from_db(r["last_date"]),
            )
        return out

    # ---- 媒体 ----

    async def find_media_by_file_id(self, telegram_file_id: str) -> MediaDTO | None:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                """
                SELECT * FROM media
                WHERE telegram_file_id = ?
                  AND object_key IS NOT NULL
                  AND download_status = 'done'
                ORDER BY id DESC
                LIMIT 1
                """,
                (telegram_file_id,),
            )
            row = await cur.fetchone()
        return _row_to_media(dict(row)) if row else None

    async def list_media(
        self,
        *,
        channel_ids: list[int] | None = None,
        status: MediaDownloadStatus | None = None,
        media_type: MediaType | None = None,
        search: str = "",
        limit: int = 1000,
        offset: int = 0,
        sort: SortKey = SortKey.DATE,
        sort_dir: SortDir = SortDir.DESC,
    ) -> list[tuple[MessageDTO, int, MediaDTO]]:
        """与 PG 对齐:ROW_NUMBER 窗口生成 media_idx;tie-break m.id DESC, idx ASC。"""
        assert self._db is not None
        where_sql, params = _media_where_clause(channel_ids, status, media_type, search)
        sort_col, nulls_last = _MEDIA_SORT_COLUMN[sort]
        nulls_clause = " NULLS LAST" if nulls_last else ""
        sql = (
            "SELECT m.*, me.id AS media_id, me.type AS media_type,"
            "       me.mime_type, me.file_name, me.file_size, me.width, me.height,"
            "       me.duration, me.telegram_file_id, me.object_key,"
            "       me.object_backend, me.thumbnail_telegram_file_id, me.emoji,"
            "       me.download_status AS media_dl_status, me.download_error,"
            "       ROW_NUMBER() OVER (PARTITION BY m.id ORDER BY me.id) - 1 AS media_idx "
            "FROM messages m JOIN media me ON me.message_id = m.id "
            f"WHERE {where_sql} "
            f"ORDER BY {sort_col} {sort_dir.value.upper()}{nulls_clause}, m.id DESC, media_idx ASC"
        )
        if limit:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        async with self._lock:
            rows = await self._db.execute_fetchall(sql, params)

        out: list[tuple[MessageDTO, int, MediaDTO]] = []
        for r in rows:
            row = dict(r)
            media = _row_to_media(
                {
                    "type": row["media_type"],
                    "mime_type": row["mime_type"],
                    "file_name": row["file_name"],
                    "file_size": row["file_size"],
                    "width": row["width"],
                    "height": row["height"],
                    "duration": row["duration"],
                    "telegram_file_id": row["telegram_file_id"],
                    "object_key": row["object_key"],
                    "object_backend": row["object_backend"],
                    "emoji": row["emoji"],
                    "download_status": row["media_dl_status"],
                    "download_error": row["download_error"],
                    "thumbnail_telegram_file_id": row["thumbnail_telegram_file_id"],
                }
            )
            out.append((_row_to_message(row, [media]), row["media_idx"], media))
        return out

    async def count_media(
        self,
        *,
        channel_ids: list[int] | None = None,
        status: MediaDownloadStatus | None = None,
        media_type: MediaType | None = None,
        search: str = "",
    ) -> int:
        assert self._db is not None
        where_sql, params = _media_where_clause(channel_ids, status, media_type, search)
        async with self._lock:
            cur = await self._db.execute(
                f"SELECT count(*) FROM messages m "
                f"JOIN media me ON me.message_id = m.id WHERE {where_sql}",
                params,
            )
            row = await cur.fetchone()
        assert row is not None
        return row[0]

    async def count_media_by_object_key(self, object_key: str) -> int:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT count(*) FROM media WHERE object_key = ?", (object_key,)
            )
            row = await cur.fetchone()
        assert row is not None
        return row[0]

    async def count_media_by_channel(self, channel_id: int) -> int:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT count(*) FROM media me "
                "JOIN messages m ON me.message_id = m.id WHERE m.channel_id = ?",
                (channel_id,),
            )
            row = await cur.fetchone()
        assert row is not None
        return row[0]

    # ---- 缩略图 ----

    async def save_thumbnail(self, thumb: ThumbnailDTO) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                """
                INSERT INTO thumbnails
                    (channel_id, telegram_msg_id, media_idx,
                     object_key, object_backend,
                     mime_type, file_size, width, height, sha256,
                     download_status, download_error, telegram_thumb_file_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (channel_id, telegram_msg_id, media_idx) DO UPDATE SET
                    object_key = excluded.object_key,
                    object_backend = excluded.object_backend,
                    mime_type = excluded.mime_type,
                    file_size = excluded.file_size,
                    width = excluded.width,
                    height = excluded.height,
                    sha256 = excluded.sha256,
                    download_status = excluded.download_status,
                    download_error = excluded.download_error,
                    telegram_thumb_file_id = excluded.telegram_thumb_file_id
                """,
                (
                    thumb.channel_id,
                    thumb.telegram_msg_id,
                    thumb.media_idx,
                    thumb.object_key,
                    thumb.object_backend,
                    thumb.mime_type,
                    thumb.file_size,
                    thumb.width,
                    thumb.height,
                    thumb.sha256,
                    thumb.download_status.value,
                    thumb.download_error,
                    thumb.telegram_thumb_file_id,
                ),
            )

    async def get_thumbnail(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> ThumbnailDTO | None:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM thumbnails "
                "WHERE channel_id = ? AND telegram_msg_id = ? AND media_idx = ?",
                (channel_id, telegram_msg_id, media_idx),
            )
            row = await cur.fetchone()
        return _row_to_thumbnail(dict(row)) if row else None

    async def delete_thumbnail(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "DELETE FROM thumbnails "
                "WHERE channel_id = ? AND telegram_msg_id = ? AND media_idx = ?",
                (channel_id, telegram_msg_id, media_idx),
            )

    async def list_thumbnails_for_message(
        self,
        channel_id: int,
        telegram_msg_id: int,
    ) -> list[ThumbnailDTO]:
        assert self._db is not None
        async with self._lock:
            rows = await self._db.execute_fetchall(
                "SELECT * FROM thumbnails "
                "WHERE channel_id = ? AND telegram_msg_id = ? ORDER BY media_idx",
                (channel_id, telegram_msg_id),
            )
        return [_row_to_thumbnail(dict(r)) for r in rows]

    async def count_thumbnails_by_object_key(self, object_key: str) -> int:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT count(*) FROM thumbnails WHERE object_key = ?", (object_key,)
            )
            row = await cur.fetchone()
        assert row is not None
        return row[0]

    # ---- 用户元数据 ----

    async def set_favorite(self, channel_id: int, telegram_msg_id: int, value: bool) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "UPDATE messages SET is_favorite = ? WHERE channel_id = ? AND telegram_msg_id = ?",
                (value, channel_id, telegram_msg_id),
            )

    async def set_tags(self, channel_id: int, telegram_msg_id: int, tags: list[str]) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "UPDATE messages SET tags = ? WHERE channel_id = ? AND telegram_msg_id = ?",
                (json.dumps(tags), channel_id, telegram_msg_id),
            )

    async def set_notes(self, channel_id: int, telegram_msg_id: int, notes: str) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "UPDATE messages SET notes = ? WHERE channel_id = ? AND telegram_msg_id = ?",
                (notes, channel_id, telegram_msg_id),
            )

    async def list_favorites(self) -> list[MessageDTO]:
        """跨频道 is_favorite=1,date DESC;media 不带(轻量路径,与 PG 对齐)。"""
        assert self._db is not None
        async with self._lock:
            rows = await self._db.execute_fetchall(
                "SELECT * FROM messages WHERE is_favorite = 1 ORDER BY date DESC"
            )
        return [_row_to_message(dict(r), []) for r in rows]

    async def list_by_tag(self, tag: str) -> list[MessageDTO]:
        """tag 精确匹配(JSON1 json_each),date DESC。"""
        assert self._db is not None
        async with self._lock:
            rows = await self._db.execute_fetchall(
                "SELECT * FROM messages "
                "WHERE EXISTS (SELECT 1 FROM json_each(messages.tags) WHERE value = ?) "
                "ORDER BY date DESC",
                (tag,),
            )
        return [_row_to_message(dict(r), []) for r in rows]
