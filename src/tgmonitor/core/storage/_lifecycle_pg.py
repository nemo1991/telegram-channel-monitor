"""PostgreSQL 生命周期子模块 — 2026-10-16 plan-batch8-1.1。

持 schema SQL 字面量(`SCHEMA_SQL` / `EXPECTED_SCHEMA` / `EXPECTED_TABLES`)
与 6 个 lifecycle 异步方法(`connect` / `close` / `init_schema` /
`introspect_schema` / `repair_schema` / `ping`)。

落地模式:free function + `self` 透传(与 plan-batch6 media_service
1:1 对齐)。`PostgresRepository` facade 持 `self._pool`,本 module
每个 method 首行 `assert self._pool is not None` 后调
`async with self._pool.acquire() as conn`。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import asyncpg

from tgmonitor.core.storage.schema_report import ColumnDrift, SchemaReport

if TYPE_CHECKING:
    from tgmonitor.core.storage.postgres_repo import PostgresRepository

log = logging.getLogger(__name__)


# 2026-10-12 plan-batch3 目标 3 step 3.1:`SCHEMA_SQL` 由 `storage/schema.sql`
# 文件内联到本模块,避免冷启动 `Path(__file__).parent / "schema.sql"`
# 文件 IO 失败路径。`init_schema()` 直接 `await conn.execute(SCHEMA_SQL)`。
# 与 `EXPECTED_SCHEMA` 单一对照源(以下紧随)— 两处 schema 描述同步改;
# `tests/test_introspect_repair.py::test_expected_schema_matches_pg_sql_columns`
# 验证两者对齐(同步改 schema 时测试会跑过)。
SCHEMA_SQL = """\
-- PostgreSQL 参考 DDL(Mongo 不固定 schema,仅以此为逻辑对照)
-- 幂等,init_schema() 会在启动时执行。

-- 2026-09-03 v1.6.0 PR #Q1:pg_trgm GIN 索引加速 LIKE 搜索
-- 需 PG ≥9.1,superuser 权限(heroku / RDS / GCP Cloud SQL / 阿里 RDS
-- 默认允许;self-hosted 首次部署需手动 `CREATE EXTENSION pg_trgm;`)。
-- init_schema() 检测到 PermissionDenied → log warning + 跳过索引创建,
-- 功能仍可用(LOWER LIKE 仍工作,只是慢)。生产部署指引见 README 段。
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS channels (
    id              BIGINT PRIMARY KEY,              -- Telegram chat_id
    title           TEXT        NOT NULL,
    username        TEXT,
    kind            TEXT        NOT NULL DEFAULT 'channel',
    member_count    INTEGER,
    created_at      TIMESTAMPTZ,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    subscribed      BOOLEAN     NOT NULL DEFAULT FALSE,
    last_synced_at  TIMESTAMPTZ
);

-- 兼容旧库:已存在的 channels 表补 subscribed / last_synced_at 列(IF NOT EXISTS 幂等)。
-- subscribed 默认 TRUE 保留"存即订"语义 — 旧用户不会被升级变成未订阅。
ALTER TABLE channels ADD COLUMN IF NOT EXISTS subscribed BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE channels ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ;
-- 2026-09-03 v1.6.0 PR #Q2:`updateChatPhoto` 推本地路径缓存 — UI 实时刷新头像
-- 用。IF NOT EXISTS 幂等,旧库自动迁移加列。
ALTER TABLE channels ADD COLUMN IF NOT EXISTS photo_local_key TEXT;
-- 2026-09-04 v1.6.4:spammer 过滤 UI 用。`is_verified` / `is_scam` / `is_fake`
-- 是 TDLib Supergroup 顶层字段;`has_protected_content` 是 Chat 顶层字段。
-- 旧库 roundtrip 默认 FALSE,UI 卡片不显示徽标。
ALTER TABLE channels ADD COLUMN IF NOT EXISTS is_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE channels ADD COLUMN IF NOT EXISTS is_scam BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE channels ADD COLUMN IF NOT EXISTS is_fake BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE channels ADD COLUMN IF NOT EXISTS has_protected_content BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS messages (
    id                  BIGSERIAL PRIMARY KEY,
    channel_id          BIGINT      NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    telegram_msg_id     BIGINT      NOT NULL,
    author              TEXT,
    date                TIMESTAMPTZ NOT NULL,
    text                TEXT        NOT NULL DEFAULT '',
    views               INTEGER,
    forwards            INTEGER,
    reply_to_msg_id     BIGINT,
    edited              BOOLEAN     NOT NULL DEFAULT FALSE,
    raw                 JSONB,
    UNIQUE (channel_id, telegram_msg_id)
);

-- 2026-08-27 v1.4.0 PR #9:补 4 个 TDLib Message 字段(老库迁移 IF NOT EXISTS 幂等)。
ALTER TABLE messages ADD COLUMN IF NOT EXISTS forward_origin   JSONB;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS via_bot_user_id  BIGINT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_album_id   BIGINT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_pinned        BOOLEAN NOT NULL DEFAULT FALSE;

-- 2026-08-27 v1.4.0 PR #10:reactions 列表 — TDLib MessageInteractionInfo 推 reactions
-- 时落库用。JSONB 存 `list[ReactionDTO.to_dict()]`;空 list 也存 `[]` 表示已
-- 清空(与 None 区分 → None 表示从未推送过)。
ALTER TABLE messages ADD COLUMN IF NOT EXISTS reactions JSONB;

-- 2026-09-09 v1.7.2:用户元数据 — 本地存储,不与 TG 同步。
-- is_favorite 单 bool;tags 用 TEXT[] 支持 contains 查询;notes 单行文本。
-- IF NOT EXISTS 幂等,旧库自动迁移。
ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_favorite BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS tags        TEXT[]    NOT NULL DEFAULT '{}';
ALTER TABLE messages ADD COLUMN IF NOT EXISTS notes       TEXT      NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_messages_channel_date
    ON messages (channel_id, date);

CREATE INDEX IF NOT EXISTS idx_messages_date
    ON messages (date);

-- 2026-09-03 v1.6.0 PR #Q1:全文搜索加速(v1.5.1 PR #B2 引入的 LOWER LIKE 全表扫)。
-- trgm GIN 把每行 text 拆成 3-gram,子串匹配走索引(B-tree LIKE '%x%' 走不到)。
-- 既有 SQL `LOWER(text) LIKE ...` 不动,planner 自动命中本索引。
CREATE INDEX IF NOT EXISTS idx_messages_text_trgm
    ON messages USING gin (lower(text) gin_trgm_ops);

CREATE TABLE IF NOT EXISTS media (
    id                  BIGSERIAL PRIMARY KEY,
    message_id          BIGINT      NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    type                TEXT        NOT NULL,
    mime_type           TEXT,
    file_name           TEXT,
    file_size           BIGINT,
    width               INTEGER,
    height              INTEGER,
    duration            INTEGER,
    telegram_file_id    TEXT,
    object_key          TEXT,
    object_backend      TEXT,
    emoji               TEXT
);

-- 兼容旧库:已存在的 media 表补 emoji 列(IF NOT EXISTS 幂等)。
ALTER TABLE media ADD COLUMN IF NOT EXISTS emoji TEXT;
-- 2026-09-29 v1.10.0:旧 v1.9.x 的 thumb_key / thumb_backend 列已废弃
-- (代码不再读也不再写,缩略图改走独立 thumbnails 表),DROP IF EXISTS 让
-- introspect_schema fresh-DB 报告 ok。旧 v1.9.x 库的这两列里只有死引用,
-- 真 thumb bytes 从未被下载过,丢零业务数据。
ALTER TABLE media DROP COLUMN IF EXISTS thumb_key;
ALTER TABLE media DROP COLUMN IF EXISTS thumb_backend;
-- 下载状态列(异步下载队列写入;旧库无此列,IF NOT EXISTS 幂等)。
ALTER TABLE media ADD COLUMN IF NOT EXISTS download_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE media ADD COLUMN IF NOT EXISTS download_error TEXT;
-- 2026-09-29:TG 端 Thumbnail.file.id(独立于原图 file.id)— MediaDownloader
-- 用它按需下载缩略图入独立 thumbnails 表 + thumb/ prefix。旧库无,NULL 兜底
-- 表示「该 media 没缩略图或 TG 端 file.id 已丢」。
ALTER TABLE media ADD COLUMN IF NOT EXISTS thumbnail_telegram_file_id TEXT;
-- 历史数据迁移:已有 object_key 的行视为已下载(done);其余保持 pending,
-- 切到 FULL 策略后 backfill 会对 pending 媒体重新触发下载。
UPDATE media SET download_status = 'done'
    WHERE object_key IS NOT NULL AND download_status = 'pending';

CREATE INDEX IF NOT EXISTS idx_media_message
    ON media (message_id);

-- 跨消息媒体去重:find_media_by_file_id 用 partial index,只索引已下载成功的行
-- (object_key IS NOT NULL),缩小索引体积、提升查询效率。绝大多数未下载的
-- media 行不会被收录。
CREATE INDEX IF NOT EXISTS idx_media_telegram_file_id
    ON media (telegram_file_id) WHERE object_key IS NOT NULL;

-- 2026-09-03 v1.6.0 PR #Q1:file_name 子串搜索加速。list_messages(search=...) 命中
-- `EXISTS (SELECT 1 FROM media WHERE message_id=m.id AND LOWER(file_name) LIKE ...)`。
-- trgm GIN 把 file_name 拆 3-gram,与 idx_messages_text_trgm 对齐。
CREATE INDEX IF NOT EXISTS idx_media_file_name_trgm
    ON media USING gin (lower(file_name) gin_trgm_ops);

CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

-- 2026-09-29:缩略图独立表(原 `media.thumb_key` / `media.thumb_backend` 列
-- 已死:写死 `"local"` 且从未真下载;读路径用 `media.object_backend` 读
-- `media.thumb_key` 解耦)。新方案:
--   - ObjectStore 端:`thumb/<sha256>.<ext>` 独立顶层 prefix
--   - Storage 端:`thumbnails` 表,关联键 `(channel_id, telegram_msg_id,
--     media_idx)` 三元组,跟父 `MessageDTO.media[media_idx]` 1:1
--   - FK 走 messages 表的 UNIQUE (channel_id, telegram_msg_id),消息删
--     时级联删除 thumb(CASCADE 应用层在 delete_message 内手工调 +
--     PG ON DELETE CASCADE 兜底)
CREATE TABLE IF NOT EXISTS thumbnails (
    channel_id               BIGINT  NOT NULL,
    telegram_msg_id          BIGINT  NOT NULL,
    media_idx                INTEGER NOT NULL,
    object_key               TEXT,
    object_backend           TEXT,
    mime_type                TEXT,
    file_size                BIGINT,
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

-- reconcile_orphans 用:已被 thumb 表引用的 object_key 不算孤儿,
-- 部分索引缩小体积(只索引已下载成功的行)。
CREATE INDEX IF NOT EXISTS idx_thumbnails_object_key
    ON thumbnails (object_key) WHERE object_key IS NOT NULL;
"""


# 2026-10-12 plan-batch3 目标 3 step 3.1:`EXPECTED_SCHEMA` / `EXPECTED_TABLES`
# 从 `storage/expected_schema.py` 内联到本模块。`introspect_schema()` 走本
# 模块的 dict 比对 `information_schema.columns`,与 `SCHEMA_SQL` 单一对照源
# 同步维护(测试 `test_introspect_repair.py::test_expected_schema_matches_pg_sql_columns`
# 守门)。
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
        # 2026-09-29:thumb_key / thumb_backend 字段 schema 保留(不读不写,
        # 留给未来清理周期);thumbnail_telegram_file_id 是 MediaDownloader
        # 用它按需下载缩略图用。
        "thumbnail_telegram_file_id": "TEXT",
        "emoji": "TEXT",
        "download_status": "TEXT",
        "download_error": "TEXT",
    },
    "meta": {
        "key": "TEXT",
        "value": "TEXT",
    },
    # 2026-09-29:缩略图独立表 — 关联键 `(channel_id, telegram_msg_id,
    # media_idx)` 三元组;FK 走 messages 表 UNIQUE (channel_id,
    # telegram_msg_id)。新代码一律走此表;`media.thumb_key/thumb_backend`
    # 字段保留但已废弃。
    "thumbnails": {
        "channel_id": "BIGINT",
        "telegram_msg_id": "BIGINT",
        "media_idx": "INTEGER",
        "object_key": "TEXT",
        "object_backend": "TEXT",
        "mime_type": "TEXT",
        "file_size": "BIGINT",
        "width": "INTEGER",
        "height": "INTEGER",
        "sha256": "TEXT",
        "download_status": "TEXT",
        "download_error": "TEXT",
        "telegram_thumb_file_id": "TEXT",
    },
}

EXPECTED_TABLES: frozenset[str] = frozenset(EXPECTED_SCHEMA.keys())


# ---- helper(只在 introspect_schema 内用) ----


def _normalize_actual(dtype: str, udt: str) -> str:
    """`information_schema.columns.data_type` → SQL 习惯名归一化。

    - 'TIMESTAMP WITH TIME ZONE' → 'TIMESTAMPTZ'
    - ARRAY 配 udt_name='_text' → 'TEXT'
    """
    d = dtype.upper()
    if d == "TIMESTAMP WITH TIME ZONE":
        return "TIMESTAMPTZ"
    if d == "ARRAY":
        return udt.lstrip("_").upper()
    return d


def _normalize_expected(t: str) -> str:
    """`EXPECTED_SCHEMA` 字符串 → 与 actual 对齐的 PG 内部名。

    - 'TEXT[]' / 'INTEGER[]' / 'BIGINT[]' → 元素类型名
    - 'BIGSERIAL' / 'SERIAL' → 'BIGINT' / 'INTEGER'(BIGSERIAL 等价
      BIGINT + sequence;information_schema 不暴露序列身份)
    """
    if t.endswith("[]"):
        return t[:-2]
    if t == "BIGSERIAL":
        return "BIGINT"
    if t == "SERIAL":
        return "INTEGER"
    return t


# ---- 6 lifecycle method ----


async def connect(self: PostgresRepository) -> None:
    """建 asyncpg 连接池(1-10 个连接);后续 query 都从池里取。"""
    self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10)


async def close(self: PostgresRepository) -> None:
    """关连接池;幂等(None 时 no-op)。"""
    if self._pool is not None:
        await self._pool.close()
        self._pool = None


async def init_schema(self: PostgresRepository) -> None:
    """执行 SCHEMA_SQL(创建表 + 索引);幂等(全 IF NOT EXISTS)。

    2026-09-03 v1.6.0 PR #Q1:`CREATE EXTENSION pg_trgm` 需 superuser,
    旧 PG 托管或受限账号没权限 → permission denied。**失败不抛**,把
    `CREATE EXTENSION` 单独跑 + log warning,后续 GIN 索引创建失败同
    样吞掉;`LOWER LIKE` 全表扫仍可用(只是慢)。生产部署由 README 段
    指引用户首次手动授权。

    2026-10-12 plan-batch3 目标 3 step 3.1:从 `storage/schema.sql` 文件
    inline 到本模块 `SCHEMA_SQL`(模块顶部三引号字符串),`init_schema`
    直接 `await conn.execute(SCHEMA_SQL)`,免去冷启动 `Path(...) / "schema.sql"`
    文件 IO 失败路径。

    2026-10-16 plan-batch8-1.1:方法体下沉到 `_lifecycle_pg.py`。
    """
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        try:
            await conn.execute(SCHEMA_SQL)
        except Exception as exc:
            # 2026-09-03 v1.6.0 PR #Q1:最常见原因是 superuser 权限不足
            # (pg_trgm GIN 索引需要)。log.warning 不抛 — `LOWER LIKE`
            # 全表扫仍是合法行为,只是性能降级。生产环境若需要 GIN 加速
            # 由 README 段引导手动 `CREATE EXTENSION pg_trgm`。
            log.warning(
                "init_schema 部分失败(可能是 pg_trgm 权限不足,功能仍可用): %s",
                exc,
            )


async def introspect_schema(self: PostgresRepository) -> SchemaReport:
    """2026-09-23 v1.8.x:查 `information_schema` 比对 EXPECTED_SCHEMA。

    两侧归一化,使 EXPECTED 用「语义直观」的类型名(`TIMESTAMPTZ` /
    `BIGSERIAL` / `TEXT[]`)而 PG 返回的 `information_schema.columns.data_type`
    用「PG 内部」名(`TIMESTAMP WITH TIME ZONE` / `BIGINT` / `ARRAY`)。
    对齐规则:
    - actual:`'TIMESTAMP WITH TIME ZONE'` → `'TIMESTAMPTZ'`(PG 内部完整名
      缩成 SQL 习惯名)
    - actual:ARRAY 配 udt_name='_text' → `'TEXT'`(剥下划线前缀大写)
    - expected:`'BIGSERIAL'` → `'BIGINT'`(BIGSERIAL 就是 BIGINT+sequence,
      information_schema 不暴露序列身份)
    - expected:`'TEXT[]'` / `'INTEGER[]'` → 元素类型名(与 actual ARRAY
      归一化对称)

    唯一索引 / GIN 不查(init_schema 已建;introspect 只关心列结构)。

    Returns:
        SchemaReport 列出 missing_tables / missing_columns /
        wrong_types / extra_columns。
    """
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        actual_tables = {
            r["table_name"]
            for r in await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            )
        }
        rows = await conn.fetch(
            "SELECT table_name, column_name, data_type, udt_name "
            "FROM information_schema.columns WHERE table_schema='public'"
        )

    actual_cols: dict[tuple[str, str], str] = {}
    for r in rows:
        actual_cols[(r["table_name"], r["column_name"])] = _normalize_actual(
            r["data_type"], r["udt_name"]
        )

    missing_tables = sorted(EXPECTED_TABLES - actual_tables)
    missing_columns: list[tuple[str, str, str]] = []
    wrong_types: list[ColumnDrift] = []
    extra_columns: list[tuple[str, str, str]] = []

    for t, cols in EXPECTED_SCHEMA.items():
        if t in missing_tables:
            continue
        for c, expected_raw in cols.items():
            actual = actual_cols.get((t, c))
            expected = _normalize_expected(expected_raw)
            if actual is None:
                missing_columns.append((t, c, expected_raw))
            elif actual != expected:
                wrong_types.append(ColumnDrift(t, c, expected_raw, actual))
    for (t, c), actual in actual_cols.items():
        if t in EXPECTED_SCHEMA and c not in EXPECTED_SCHEMA[t]:
            extra_columns.append((t, c, actual))

    return SchemaReport(
        missing_tables=missing_tables,
        missing_columns=missing_columns,
        wrong_types=wrong_types,
        extra_columns=extra_columns,
    )


async def repair_schema(self: PostgresRepository, report: SchemaReport) -> None:
    """2026-09-23 v1.8.x:基于 report 跑幂等修复。

    - `missing_columns` → `ALTER TABLE ADD COLUMN IF NOT EXISTS`(幂等 / 无锁)
    - `missing_tables` → `raise RuntimeError`(避免 DROP+CREATE 丢数据;
      运维手动跑 init_schema 即可)
    - `wrong_types` → 仅 `log.warning`(类型 ALTER 重写表对大表
      lock-heavy,不动)
    - `extra_columns` → 仅 `log.info`(诊断信息,可能 legacy schema)

    第二次 `introspect_schema()` 应返 ok=True(idempotent)。
    """
    assert self._pool is not None
    if report.missing_tables:
        raise RuntimeError(
            f"schema repair: missing tables {report.missing_tables} — "
            "manual init_schema() required (auto-repair refuses DROP+CREATE)"
        )
    if not report.missing_columns:
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
        return

    async with self._pool.acquire() as conn, conn.transaction():
        for t, c, expected in report.missing_columns:
            log.warning("schema repair: adding missing column %s.%s (%s)", t, c, expected)
            await conn.execute(f'ALTER TABLE "{t}" ADD COLUMN IF NOT EXISTS "{c}" {expected}')
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


async def ping(self: PostgresRepository) -> bool:
    """SELECT 1 探活;任何异常返 False。"""
    assert self._pool is not None
    try:
        async with self._pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False
