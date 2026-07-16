-- PostgreSQL 参考 DDL(Mongo 不固定 schema,仅以此为逻辑对照)
-- 幂等,init_schema() 会在启动时执行。

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

CREATE INDEX IF NOT EXISTS idx_messages_channel_date
    ON messages (channel_id, date);

CREATE INDEX IF NOT EXISTS idx_messages_date
    ON messages (date);

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

CREATE INDEX IF NOT EXISTS idx_media_message
    ON media (message_id);

CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
