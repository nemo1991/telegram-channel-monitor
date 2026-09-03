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
    thumb_key           TEXT,
    thumb_backend       TEXT,
    emoji               TEXT
);

-- 兼容旧库:已存在的 media 表补 emoji 列(IF NOT EXISTS 幂等)。
ALTER TABLE media ADD COLUMN IF NOT EXISTS emoji TEXT;
-- 下载状态列(异步下载队列写入;旧库无此列,IF NOT EXISTS 幂等)。
ALTER TABLE media ADD COLUMN IF NOT EXISTS download_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE media ADD COLUMN IF NOT EXISTS download_error TEXT;
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
