"""PG 频道子模块 — 2026-10-16 plan-batch8-1.3。

持 13 个方法(11 个频道域 + 2 个 meta 域:`upsert_channel` / `upsert_channel_metadata` /
`update_channel_metadata` / `set_channel_monitored` / `list_channels` /
`list_monitored_channels` / `get_channel` / `delete_channel` /
`get_max_telegram_msg_id` / `get_meta` / `set_meta`)+ 顶部
`_row_to_channel` 反序列化 helper。

落地模式:free function + `self` 透传(与 plan-batch8-1.1/1.2
`_lifecycle_pg` / `_user_meta_pg` 1:1 对齐)。`PostgresRepository` facade
持 `self._pool`,本 module 走 `self._pool.acquire() as conn` 拿连接。

`_row_to_channel` 是频道反序列化的唯一定义点,被本 module 4 处使用
(list_channels / list_monitored_channels / get_channel + facade 自身
后续如有别处引用),放本 module 顶部,与频道 method 同生命周期。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.storage._sqlite_common import _ensure_utc

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
        # 物理列 monitored 承载"监听"语义(2.0.6 由 subscribed 正名)。
        is_monitored=bool(row.get("monitored", False)),
        # v2.0.3:TG 订阅状态;旧库无此列时默认 False(不回填,靠刷新校正)。
        is_tg_subscribed=bool(row.get("tg_subscribed", False)),
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
    """全字段 upsert(含 monitored) — 老调用方兼容;**新代码走 upsert_channel_metadata**。

    2026-09-03 v1.6.0 PR #Q2:加 `photo_local_key` 字段。
    2026-09-04 v1.6.4:加 4 个 spammer 过滤字段。
    """
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO channels
                (id, title, username, kind, member_count, created_at,
                 monitored, tg_subscribed, last_synced_at, photo_local_key,
                 is_verified, is_scam, is_fake, has_protected_content)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                username = EXCLUDED.username,
                kind = EXCLUDED.kind,
                member_count = EXCLUDED.member_count,
                created_at = EXCLUDED.created_at,
                monitored = EXCLUDED.monitored,
                tg_subscribed = EXCLUDED.tg_subscribed,
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
            # 2026-11-25 P2-7:naive datetime 视为 UTC(P2-7 全 PG 写路径统一)。
            _ensure_utc(channel.created_at),
            channel.is_monitored,
            channel.is_tg_subscribed,
            _ensure_utc(channel.last_synced_at),
            channel.photo_local_key,
            channel.is_verified,
            channel.is_scam,
            channel.is_fake,
            channel.has_protected_content,
        )


async def upsert_channel_metadata(self: PostgresRepository, channel: ChannelDTO) -> None:
    """只更元数据字段;两个订阅/监听标志保持旧值(sync 用)。

    v2.0.3:tg_subscribed 只在首次插入写 caller 值,已存在行不动 — 刷新落库校正
    走专门的 `set_channel_tg_subscribed`。
    2026-09-03 v1.6.0 PR #Q2:加 `photo_local_key`。
    2026-09-04 v1.6.4:加 4 个 spammer 过滤字段。
    """
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO channels
                (id, title, username, kind, member_count, created_at,
                 monitored, tg_subscribed, last_synced_at, photo_local_key,
                 is_verified, is_scam, is_fake, has_protected_content)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
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
            _ensure_utc(channel.created_at),
            # 首次插入给默认值(False),后续 DO UPDATE 两个标志都不动
            channel.is_monitored,
            channel.is_tg_subscribed,
            _ensure_utc(channel.last_synced_at),
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


async def set_channel_monitored(self: PostgresRepository, channel_id: int, monitored: bool) -> None:
    """只设**监听**标志(物理列 monitored);未建档时建 stub。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        # 不存在就先建一条 stub
        await conn.execute(
            """
            INSERT INTO channels (id, title, monitored)
            VALUES ($1, $2, $3)
            ON CONFLICT (id) DO UPDATE SET monitored = EXCLUDED.monitored
            """,
            channel_id,
            f"#{channel_id}",
            monitored,
        )


async def set_channel_tg_subscribed(
    self: PostgresRepository, channel_id: int, tg_subscribed: bool
) -> None:
    """只设 **TG 订阅**标志,不动监听标志(v2.0.3 刷新落库校正用)。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO channels (id, title, tg_subscribed)
            VALUES ($1, $2, $3)
            ON CONFLICT (id) DO UPDATE SET tg_subscribed = EXCLUDED.tg_subscribed
            """,
            channel_id,
            f"#{channel_id}",
            tg_subscribed,
        )


async def upsert_channel_and_set_monitored(self: PostgresRepository, channel: ChannelDTO) -> None:
    """P2 #37:全字段 upsert + monitored 强制 TRUE(单条语句,天然原子)。

    与 `upsert_channel` 唯一差异:monitored 列写 TRUE 而非 caller 值 —
    替代旧 `upsert_channel + set_channel_monitored` 两步组合。
    """
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO channels
                (id, title, username, kind, member_count, created_at,
                 monitored, tg_subscribed, last_synced_at, photo_local_key,
                 is_verified, is_scam, is_fake, has_protected_content)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                username = EXCLUDED.username,
                kind = EXCLUDED.kind,
                member_count = EXCLUDED.member_count,
                created_at = EXCLUDED.created_at,
                monitored = TRUE,
                tg_subscribed = EXCLUDED.tg_subscribed,
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
            _ensure_utc(channel.created_at),
            channel.is_tg_subscribed,
            _ensure_utc(channel.last_synced_at),
            channel.photo_local_key,
            channel.is_verified,
            channel.is_scam,
            channel.is_fake,
            channel.has_protected_content,
        )


async def upsert_channel_metadata_and_set_tg_subscribed(
    self: PostgresRepository, channel: ChannelDTO
) -> None:
    """P2 #37:只更元数据 + tg_subscribed 强制 TRUE(单条语句,天然原子)。

    与 `upsert_channel_metadata` 的差异:DO UPDATE 额外把 tg_subscribed
    置 TRUE;monitored 标志保持旧值(与 metadata-only 语义一致)。
    """
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO channels
                (id, title, username, kind, member_count, created_at,
                 monitored, tg_subscribed, last_synced_at, photo_local_key,
                 is_verified, is_scam, is_fake, has_protected_content)
            VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                username = EXCLUDED.username,
                kind = EXCLUDED.kind,
                member_count = EXCLUDED.member_count,
                created_at = EXCLUDED.created_at,
                tg_subscribed = TRUE,
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
            _ensure_utc(channel.created_at),
            channel.is_monitored,
            _ensure_utc(channel.last_synced_at),
            channel.photo_local_key,
            channel.is_verified,
            channel.is_scam,
            channel.is_fake,
            channel.has_protected_content,
        )


async def set_channel_flags(
    self: PostgresRepository,
    channel_id: int,
    *,
    monitored: bool | None = None,
    tg_subscribed: bool | None = None,
) -> None:
    """P2 #37:双标志原子设置(conn.transaction() 单事务,None=不动)。

    旧路径两条独立语句,中途异常留「TG 已退订、监听仍开」中间态;包进
    单事务后要么全落、要么全不落。未建档时沿用 set_* 的 id-stub 语义。
    """
    assert self._pool is not None
    async with self._pool.acquire() as conn, conn.transaction():
        if tg_subscribed is not None:
            await conn.execute(
                """
                INSERT INTO channels (id, title, tg_subscribed)
                VALUES ($1, $2, $3)
                ON CONFLICT (id) DO UPDATE SET tg_subscribed = EXCLUDED.tg_subscribed
                """,
                channel_id,
                f"#{channel_id}",
                tg_subscribed,
            )
        if monitored is not None:
            await conn.execute(
                """
                INSERT INTO channels (id, title, monitored)
                VALUES ($1, $2, $3)
                ON CONFLICT (id) DO UPDATE SET monitored = EXCLUDED.monitored
                """,
                channel_id,
                f"#{channel_id}",
                monitored,
            )


async def count_tg_subscribed(self: PostgresRepository) -> int:
    """TG 侧仍订阅/加入的频道数(大盘"已加入频道"卡)。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM channels WHERE tg_subscribed = TRUE")


async def list_tg_subscribed_channels(self: PostgresRepository) -> list[ChannelDTO]:
    """只返 tg_subscribed=TRUE 的频道(按 id;2026-11-19 左栏启动读库用)。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, title, username, kind, member_count, created_at, "
            "monitored, tg_subscribed, last_synced_at, photo_local_key, "
            "is_verified, is_scam, is_fake, has_protected_content "
            "FROM channels WHERE tg_subscribed = TRUE ORDER BY id"
        )
    return [_row_to_channel(r) for r in rows]


async def list_channels(self: PostgresRepository) -> list[ChannelDTO]:
    """所有频道(按 id 升序);含未订阅的。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, title, username, kind, member_count, created_at, "
            "monitored, tg_subscribed, last_synced_at, photo_local_key, "
            "is_verified, is_scam, is_fake, has_protected_content "
            "FROM channels ORDER BY id"
        )
    return [_row_to_channel(r) for r in rows]


async def list_monitored_channels(self: PostgresRepository) -> list[ChannelDTO]:
    """只返 monitored=TRUE 的频道(按 id 升序);供 MonitorService 喂白名单。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, title, username, kind, member_count, created_at, "
            "monitored, tg_subscribed, last_synced_at, photo_local_key, "
            "is_verified, is_scam, is_fake, has_protected_content "
            "FROM channels WHERE monitored = TRUE ORDER BY id"
        )
    return [_row_to_channel(r) for r in rows]


async def get_channel(self: PostgresRepository, channel_id: int) -> ChannelDTO | None:
    """单频道;不存在返 None。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, title, username, kind, member_count, created_at, "
            "monitored, tg_subscribed, last_synced_at, photo_local_key, "
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
