"""SQLite 运维活动子模块 — 2026-11-25 P2-N1 按域拆分。

持 2 个 activity method（`append_activity` / `list_activities`），写 `activities`
表。落地模式：free function + `self` 透传（与 `_activity_pg` 1:1 对齐）。
`SqliteRepository` facade 持 `self._db` / `self._lock`，本 module 直接透传使用。

`occurred_at` 用 UTC ISO 字符串序列化（与 PG 的 TIMESTAMPTZ 在 DTO 层归一）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tgmonitor.core.dto import ActivityDTO
from tgmonitor.core.storage._sqlite_common import _dt_from_db, _dt_to_db

if TYPE_CHECKING:
    from tgmonitor.core.storage.sqlite_repo import SqliteRepository


async def append_activity(self: SqliteRepository, activity: ActivityDTO) -> None:
    """追加一条活动 — 全留存，不裁剪（用户 2026-11-17 决策）。"""
    assert self._db is not None
    async with self._lock:
        await self._db.execute(
            "INSERT INTO activities "
            "(event_type, occurred_at, icon, title, detail, severity, channel_id, "
            "telegram_msg_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                activity.event_type,
                _dt_to_db(activity.occurred_at),
                activity.icon,
                activity.title,
                activity.detail,
                activity.severity,
                activity.channel_id,
                activity.telegram_msg_id,
            ),
        )


async def list_activities(self: SqliteRepository, limit: int = 200) -> list[ActivityDTO]:
    """列最近 N 条 — occurred_at DESC, id DESC（新的在前）。"""
    assert self._db is not None
    async with self._lock:
        rows = await self._db.execute_fetchall(
            "SELECT * FROM activities ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (limit,),
        )
    return [
        ActivityDTO(
            id=r["id"],
            event_type=r["event_type"],
            occurred_at=_dt_from_db(r["occurred_at"]) or datetime.now(UTC),
            icon=r["icon"],
            title=r["title"],
            detail=r["detail"],
            severity=r["severity"],
            channel_id=r["channel_id"],
            telegram_msg_id=r["telegram_msg_id"],
        )
        for r in rows
    ]
