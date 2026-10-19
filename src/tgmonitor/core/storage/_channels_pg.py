"""PG 频道子模块 — 2026-10-16 plan-batch8-1.3。

持 11 个频道 + meta method(`upsert_channel` / `upsert_channel_metadata` /
`update_channel_metadata` / `set_channel_subscribed` / `list_channels` /
`list_subscribed_channels` / `get_channel` / `delete_channel` /
`get_max_telegram_msg_id` / `get_meta` / `set_meta`)+ 顶部
`_row_to_channel` 反序列化 helper。

落地模式:free function + `self` 透传(与 plan-batch8-1.1/1.2
`_lifecycle_pg` / `_user_meta_pg` 1:1 对齐)。`PostgresRepository` facade
持 `self._pool`,本 module 走 `self._pool.acquire() as conn` 拿连接。

`_row_to_channel` 是频道反序列化的唯一定义点,被本 module 4 处使用
(list_channels / list_subscribed_channels / get_channel + facade 自身
后续如有别处引用),放本 module 顶部,与频道 method 同生命周期。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

from tgmonitor.core.dto import ChannelDTO

if TYPE_CHECKING:
    from tgmonitor.core.storage.postgres_repo import PostgresRepository


def _row_to_channel(row: asyncpg.Record) -> ChannelDTO:
    return ChannelDTO(
        id=row["id"],
        title=row["title"],
        username=row["username"],
        kind=row["kind"],
        member_count=row["member_count"],
        created_at=row["created_at"],
        is_subscribed=bool(row.get("subscribed", True)),
        last_synced_at=row.get("last_synced_at"),
        # 2026-09-03 v1.6.0 PR #Q2:旧库可能没这列,get 兜底 None
        photo_local_key=row.get("photo_local_key"),
        # 2026-09-04 v1.6.4:spammer 过滤 UI 用;旧库 roundtrip 默认 False
        is_verified=bool(row.get("is_verified", False)),
        is_scam=bool(row.get("is_scam", False)),
        is_fake=bool(row.get("is_fake", False)),
        has_protected_content=bool(row.get("has_protected_content", False)),
    )


# ---- 11 channel + meta method ----


async def upsert_channel(self: PostgresRepository, channel: ChannelDTO) -> None:
    """全字段 upsert(含 subscribed) — 老调用方兼容;**新代码走 upsert_channel_metadata**。

    2026-09-03 v1.6.0 PR #Q2:加 `photo_local_key` 字段。
    2026-09-04 v1.6.4:加 4 个 spammer 过滤字段。
    """
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO channels
                (id, title, username, kind, member_count, created_at,
                 subscribed, last_synced_at, photo_local_key,
                 is_verified, is_scam, is_fake, has_protected_content)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                username = EXCLUDED.username,
                kind = EXCLUDED.kind,
                member_count = EXCLUDED.member_count,
                created_at = EXCLUDED.created_at,
                subscribed = EXCLUDED.subscribed,
                last_synced_at = EXCLUDED.last_synced_at,
                photo_local_key = EXCLUDED.photo_local_key,
                is_verified = EXCLUDED.is_verified,
                is_scam = EXCLUDED.is_scam,
                is_fake = EXCLUDED.is_fake,
                has_protected_content = EXCLUDED.has_protected_content
            """,
            channel.id,
            channel.title,
            channel.username,
            channel.kind,
            channel.member_count,
            channel.created_at,
            channel.is_subscribed,
            channel.last_synced_at,
            channel.photo_local_key,
            channel.is_verified,
            channel.is_scam,
            channel.is_fake,
            channel.has_protected_content,
        )


async def upsert_channel_metadata(self: PostgresRepository, channel: ChannelDTO) -> None:
    """只更元数据字段;subscribed 保持旧值(sync 用)。

    2026-09-03 v1.6.0 PR #Q2:加 `photo_local_key`。
    2026-09-04 v1.6.4:加 4 个 spammer 过滤字段。
    """
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO channels
                (id, title, username, kind, member_count, created_at,
                 subscribed, last_synced_at, photo_local_key,
                 is_verified, is_scam, is_fake, has_protected_content)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                username = EXCLUDED.username,
                kind = EXCLUDED.kind,
                member_count = EXCLUDED.member_count,
                created_at = EXCLUDED.created_at,
                last_synced_at = EXCLUDED.last_synced_at,
                photo_local_key = EXCLUDED.photo_local_key,
                is_verified = EXCLUDED.is_verified,
                is_scam = EXCLUDED.is_scam,
                is_fake = EXCLUDED.is_fake,
                has_protected_content = EXCLUDED.has_protected_content
            """,
            channel.id,
            channel.title,
            channel.username,
            channel.kind,
            channel.member_count,
            channel.created_at,
            # 首次插入时给个合理默认(False),后续 DO UPDATE 不动 subscribed
            channel.is_subscribed,
            channel.last_synced_at,
            channel.photo_local_key,
            channel.is_verified,
            channel.is_scam,
            channel.is_fake,
            channel.has_protected_content,
        )


async def update_channel_metadata(
    self: PostgresRepository,
    channel_id: int,
    *,
    title: str | None = None,
    username: str | None = None,
    member_count: int | None = None,
    photo_local_key: str | None = None,
    is_verified: bool | None = None,  # 2026-09-04 v1.6.4
    is_scam: bool | None = None,
    is_fake: bool | None = None,
    has_protected_content: bool | None = None,
) -> None:
    """2026-08-27 v1.4.0 PR #14:Postgres 部分更新 — `COALESCE($N, 列名)`
    把 None 映射成「保留旧值」,只动传入字段。

    2026-09-03 v1.6.0 PR #Q2:加 `photo_local_key` 字段。
    2026-09-04 v1.6.4:加 4 个 spammer 过滤字段。
    """
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE channels SET
                title                = COALESCE($1, title),
                username             = COALESCE($2, username),
                member_count         = COALESCE($3, member_count),
                photo_local_key      = COALESCE($4, photo_local_key),
                is_verified          = COALESCE($5, is_verified),
                is_scam              = COALESCE($6, is_scam),
                is_fake              = COALESCE($7, is_fake),
                has_protected_content = COALESCE($8, has_protected_content)
            WHERE id = $9
            """,
            title,
            username,
            member_count,
            photo_local_key,
            is_verified,
            is_scam,
            is_fake,
            has_protected_content,
            channel_id,
        )


async def set_channel_subscribed(
    self: PostgresRepository, channel_id: int, subscribed: bool
) -> None:
    """只设订阅标志;频道未建档时用 id 做个 stub(后续会被 metadata 覆盖)。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        # 不存在就先建一条 stub
        await conn.execute(
            """
            INSERT INTO channels (id, title, subscribed)
            VALUES ($1, $2, $3)
            ON CONFLICT (id) DO UPDATE SET subscribed = EXCLUDED.subscribed
            """,
            channel_id,
            f"#{channel_id}",
            subscribed,
        )


async def list_channels(self: PostgresRepository) -> list[ChannelDTO]:
    """所有频道(按 id 升序);含未订阅的。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, title, username, kind, member_count, created_at, "
            "subscribed, last_synced_at, photo_local_key, "
            "is_verified, is_scam, is_fake, has_protected_content "
            "FROM channels ORDER BY id"
        )
    return [_row_to_channel(r) for r in rows]


async def list_subscribed_channels(self: PostgresRepository) -> list[ChannelDTO]:
    """只返 subscribed=TRUE 的频道(按 id 升序);供 MonitorService 喂白名单。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, title, username, kind, member_count, created_at, "
            "subscribed, last_synced_at, photo_local_key, "
            "is_verified, is_scam, is_fake, has_protected_content "
            "FROM channels WHERE subscribed = TRUE ORDER BY id"
        )
    return [_row_to_channel(r) for r in rows]


async def get_channel(self: PostgresRepository, channel_id: int) -> ChannelDTO | None:
    """单频道;不存在返 None。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, title, username, kind, member_count, created_at, "
            "subscribed, last_synced_at, photo_local_key, "
            "is_verified, is_scam, is_fake, has_protected_content "
            "FROM channels WHERE id = $1",
            channel_id,
        )
    return _row_to_channel(row) if row else None


async def delete_channel(self: PostgresRepository, channel_id: int) -> None:
    """删频道;ON DELETE CASCADE 自动带 messages / media(由 schema 定义)。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        await conn.execute("DELETE FROM channels WHERE id = $1", channel_id)


async def get_max_telegram_msg_id(self: PostgresRepository, channel_id: int) -> int | None:
    """续拉历史用 — 该频道已落库的最大 telegram_msg_id;无历史返 None。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT MAX(telegram_msg_id) FROM messages WHERE channel_id = $1",
            channel_id,
        )


async def get_meta(self: PostgresRepository, key: str) -> str | None:
    """全局单值元数据;不存在返 None。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT value FROM meta WHERE key = $1",
            key,
        )


async def set_meta(self: PostgresRepository, key: str, value: str) -> None:
    """upsert 语义:ON CONFLICT DO UPDATE 覆盖。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO meta (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            key,
            value,
        )
