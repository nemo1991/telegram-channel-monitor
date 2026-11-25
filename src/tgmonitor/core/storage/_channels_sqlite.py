"""SQLite 频道子模块 — 2026-11-25 P2-N1 按域拆分。

持 12 个 channel method + 2 个 meta method（与 `_channels_pg` 1:1 对齐）。
落地模式：free function + `self` 透传。`SqliteRepository` facade 持
`self._db` / `self._lock`，本 module 直接透传使用。

`_row_to_channel` 反序列化 helper 定义在 `_sqlite_common.py`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.storage._sqlite_common import _dt_to_db, _row_to_channel

if TYPE_CHECKING:
    from tgmonitor.core.storage.sqlite_repo import SqliteRepository


async def upsert_channel(self: SqliteRepository, channel: ChannelDTO) -> None:
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            """
            INSERT INTO channels
                (id, title, username, kind, member_count, created_at,
                 monitored, tg_subscribed, last_synced_at, photo_local_key,
                 is_verified, is_scam, is_fake, has_protected_content)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                kind = excluded.kind,
                member_count = excluded.member_count,
                created_at = excluded.created_at,
                monitored = excluded.monitored,
                tg_subscribed = excluded.tg_subscribed,
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
                channel.is_monitored,
                channel.is_tg_subscribed,
                _dt_to_db(channel.last_synced_at),
                channel.photo_local_key,
                channel.is_verified,
                channel.is_scam,
                channel.is_fake,
                channel.has_protected_content,
            ),
        )


async def upsert_channel_metadata(self: SqliteRepository, channel: ChannelDTO) -> None:
    """只更元数据;两个订阅/监听标志保持旧值(首次插入用 caller 值)。

    v2.0.3:新增 tg_subscribed 列只在首次插入写 caller 值,已存在行不动 —
    刷新落库校正 tg_subscribed 走专门的 `set_channel_tg_subscribed`。
    """
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            """
            INSERT INTO channels
                (id, title, username, kind, member_count, created_at,
                 monitored, tg_subscribed, last_synced_at, photo_local_key,
                 is_verified, is_scam, is_fake, has_protected_content)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                channel.is_monitored,
                channel.is_tg_subscribed,
                _dt_to_db(channel.last_synced_at),
                channel.photo_local_key,
                channel.is_verified,
                channel.is_scam,
                channel.is_fake,
                channel.has_protected_content,
            ),
        )


async def update_channel_metadata(
    self: SqliteRepository,
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


async def set_channel_monitored(self: SqliteRepository, channel_id: int, monitored: bool) -> None:
    """只设**监听**标志(物理列 monitored);频道未建档时用 id 做 stub。"""
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            """
            INSERT INTO channels (id, title, monitored)
            VALUES (?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET monitored = excluded.monitored
            """,
            (channel_id, f"#{channel_id}", monitored),
        )


async def set_channel_tg_subscribed(
    self: SqliteRepository, channel_id: int, tg_subscribed: bool
) -> None:
    """只设 **TG 订阅**标志(tg_subscribed 列),不动监听标志。

    v2.0.3:刷新落库校正 TG 订阅状态用;频道未建档时用 id 做 stub。
    """
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            """
            INSERT INTO channels (id, title, tg_subscribed)
            VALUES (?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET tg_subscribed = excluded.tg_subscribed
            """,
            (channel_id, f"#{channel_id}", tg_subscribed),
        )


async def upsert_channel_and_set_monitored(self: SqliteRepository, channel: ChannelDTO) -> None:
    """P2 #37:全字段 upsert + monitored 强制 True(单条语句,天然原子)。

    与 `upsert_channel` 唯一差异:monitored 列写 True 而非 caller 值 —
    替代旧 `upsert_channel + set_channel_monitored` 两步组合,消除
    「upsert 成功、set 标志失败」的半状态窗口。
    """
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            """
            INSERT INTO channels
                (id, title, username, kind, member_count, created_at,
                 monitored, tg_subscribed, last_synced_at, photo_local_key,
                 is_verified, is_scam, is_fake, has_protected_content)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                kind = excluded.kind,
                member_count = excluded.member_count,
                created_at = excluded.created_at,
                monitored = 1,
                tg_subscribed = excluded.tg_subscribed,
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
                channel.is_tg_subscribed,
                _dt_to_db(channel.last_synced_at),
                channel.photo_local_key,
                channel.is_verified,
                channel.is_scam,
                channel.is_fake,
                channel.has_protected_content,
            ),
        )


async def upsert_channel_metadata_and_set_tg_subscribed(
    self: SqliteRepository, channel: ChannelDTO
) -> None:
    """P2 #37:只更元数据 + tg_subscribed 强制 True(单条语句,天然原子)。

    与 `upsert_channel_metadata` 的差异:DO UPDATE 额外把 tg_subscribed
    置 1;monitored 标志保持旧值(与 metadata-only 语义一致)。
    """
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            """
            INSERT INTO channels
                (id, title, username, kind, member_count, created_at,
                 monitored, tg_subscribed, last_synced_at, photo_local_key,
                 is_verified, is_scam, is_fake, has_protected_content)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                kind = excluded.kind,
                member_count = excluded.member_count,
                created_at = excluded.created_at,
                tg_subscribed = 1,
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
                channel.is_monitored,
                _dt_to_db(channel.last_synced_at),
                channel.photo_local_key,
                channel.is_verified,
                channel.is_scam,
                channel.is_fake,
                channel.has_protected_content,
            ),
        )


async def set_channel_flags(
    self: SqliteRepository,
    channel_id: int,
    *,
    monitored: bool | None = None,
    tg_subscribed: bool | None = None,
) -> None:
    """P2 #37:双标志原子设置(BEGIN/COMMIT 单事务,None=不动)。

    旧路径 `set_channel_tg_subscribed(False)` + `set_channel_monitored(False)`
    两条独立语句,中途异常留「TG 已退订、监听仍开」中间态;包进单事务后
    要么全落、要么全不落。频道未建档时沿用 set_* 的 id-stub 语义。
    """
    assert self._db is not None
    async with self._lock:
        await self._db.execute("BEGIN")
        try:
            if tg_subscribed is not None:
                await self._db.execute(
                    """
                    INSERT INTO channels (id, title, tg_subscribed)
                    VALUES (?, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET tg_subscribed = excluded.tg_subscribed
                    """,
                    (channel_id, f"#{channel_id}", tg_subscribed),
                )
            if monitored is not None:
                await self._db.execute(
                    """
                    INSERT INTO channels (id, title, monitored)
                    VALUES (?, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET monitored = excluded.monitored
                    """,
                    (channel_id, f"#{channel_id}", monitored),
                )
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise


async def count_tg_subscribed(self: SqliteRepository) -> int:
    """TG 侧仍订阅/加入的频道数(大盘"已加入频道"卡,与"已监听"独立)。"""
    assert self._db is not None
    async with self._lock:
        cur = await self._db.execute("SELECT COUNT(*) FROM channels WHERE tg_subscribed = 1")
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def list_tg_subscribed_channels(self: SqliteRepository) -> list[ChannelDTO]:
    """只返 tg_subscribed=1 的频道(按 id;2026-11-19 左栏启动读库用)。"""
    assert self._db is not None
    async with self._lock:
        rows = await self._db.execute_fetchall(
            "SELECT * FROM channels WHERE tg_subscribed = 1 ORDER BY id"
        )
    return [_row_to_channel(dict(r)) for r in rows]


async def list_channels(self: SqliteRepository) -> list[ChannelDTO]:
    assert self._db is not None
    async with self._lock:
        rows = await self._db.execute_fetchall("SELECT * FROM channels ORDER BY id")
    return [_row_to_channel(dict(r)) for r in rows]


async def list_monitored_channels(self: SqliteRepository) -> list[ChannelDTO]:
    assert self._db is not None
    async with self._lock:
        rows = await self._db.execute_fetchall(
            "SELECT * FROM channels WHERE monitored = 1 ORDER BY id"
        )
    return [_row_to_channel(dict(r)) for r in rows]


async def get_channel(self: SqliteRepository, channel_id: int) -> ChannelDTO | None:
    assert self._db is not None
    async with self._lock:
        cur = await self._db.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
        row = await cur.fetchone()
    return _row_to_channel(dict(row)) if row else None


async def delete_channel(self: SqliteRepository, channel_id: int) -> None:
    """删频道;FK CASCADE 自动带 messages / media / thumbnails。"""
    assert self._db is not None
    async with self._lock:
        await self._db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))


async def get_max_telegram_msg_id(self: SqliteRepository, channel_id: int) -> int | None:
    assert self._db is not None
    async with self._lock:
        cur = await self._db.execute(
            "SELECT MAX(telegram_msg_id) FROM messages WHERE channel_id = ?",
            (channel_id,),
        )
        row = await cur.fetchone()
    assert row is not None  # MAX 聚合恒返一行(值可为 NULL)
    return row[0]


async def get_meta(self: SqliteRepository, key: str) -> str | None:
    assert self._db is not None
    async with self._lock:
        cur = await self._db.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = await cur.fetchone()
    return row[0] if row is not None else None


async def set_meta(self: SqliteRepository, key: str, value: str) -> None:
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
