"""PG 运维活动子模块 — 2026-11-17 v2.0.4 FR-05。

持 2 个 activity method（`append_activity` / `list_activities`），写 `activities`
表。落地模式：free function + `self` 透传（与 `_user_meta_pg` / `_thumbnails_pg`
1:1 对齐）。`PostgresRepository` facade 持 `self._pool`，本 module 走
`self._pool.acquire() as conn` 拿连接。

`occurred_at` 用 asyncpg 原生 TIMESTAMPTZ（aware datetime 直接传入/读回），
与 SQLite 的 UTC ISO 序列化在 DTO 层归一。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tgmonitor.core.dto import ActivityDTO

if TYPE_CHECKING:
    from tgmonitor.core.storage.postgres_repo import PostgresRepository


async def append_activity(self: PostgresRepository, activity: ActivityDTO) -> None:
    """追加一条活动 — 全留存，不裁剪。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO activities "
            "(event_type, occurred_at, icon, title, detail, severity, channel_id, "
            "telegram_msg_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            activity.event_type,
            activity.occurred_at,
            activity.icon,
            activity.title,
            activity.detail,
            activity.severity,
            activity.channel_id,
            activity.telegram_msg_id,
        )


async def list_activities(self: PostgresRepository, limit: int = 200) -> list[ActivityDTO]:
    """列最近 N 条 — occurred_at DESC, id DESC（新的在前）。"""
    assert self._pool is not None
    async with self._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM activities ORDER BY occurred_at DESC, id DESC LIMIT $1",
            limit,
        )
    return [
        ActivityDTO(
            id=r["id"],
            event_type=r["event_type"],
            occurred_at=r["occurred_at"],
            icon=r["icon"],
            title=r["title"],
            detail=r["detail"],
            severity=r["severity"],
            channel_id=r["channel_id"],
            telegram_msg_id=r["telegram_msg_id"],
        )
        for r in rows
    ]
