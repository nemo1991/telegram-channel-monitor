"""活动记录归一化 + 落库订阅者 — 2026-11-17 v2.0.4 FR-04/FR-05。

把领域事件归一化成 `ActivityDTO`，供两处共用：
1. core 侧 `EventBus.subscribe_all` 订阅者 → 写 `ActivityStore`（持久化）；
2. UI 启动时从库读取最近 N 条渲染（不再依赖内存事件时间线）。

架构约束：`core/` 禁止 import PySide6/qasync；formatter 全是纯函数，
只提取 str / int / datetime 等可序列化字段，异常对象只取 `str()` 摘要，
不塞进 DTO（RK-05 缓解）。

事件覆盖范围（D3-α 运维事件集，不含逐条消息类）：
`LoginStateChanged` / `ChannelMonitored` / `ChannelUnmonitored` /
`ChannelSyncDone` / `BatchDone` / `ExportDone` / `MediaReconcileFinished` /
`SettingsChanged` / `ErrorOccurred`（含 `AuthErrorOccurred` 子类）。
`Progress` 类与消息类事件一律不进活动账本。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from tgmonitor.core.dto import ActivityDTO
from tgmonitor.core.events import (
    BatchDone,
    ChannelMonitored,
    ChannelSyncDone,
    ChannelUnmonitored,
    ErrorOccurred,
    Event,
    ExportDone,
    LoginStateChanged,
    MediaReconcileFinished,
    SettingsChanged,
)

if TYPE_CHECKING:
    from tgmonitor.core.storage.repository import ActivityStore

log = logging.getLogger(__name__)

# detail 最长展示长度（防止超长异常/路径撑爆 UI 与 DB）
_DETAIL_MAX = 200


def _clip(value: object, limit: int = _DETAIL_MAX) -> str:
    """把任意值安全转成截断后的 str（异常对象只取 str，不保留引用）。"""
    if value is None:
        return ""
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------- 9 类事件 formatter（纯函数：event → ActivityDTO | None）----------


def _fmt_login(e: LoginStateChanged) -> ActivityDTO:
    severity = "warn" if e.state in ("error", "phone_required") else "info"
    return ActivityDTO(
        event_type="login_state_changed",
        occurred_at=e.occurred_at,
        icon="nav_live",
        title="登录状态切换",
        detail=f"{e.state} {_clip(e.detail, 60)}".strip(),
        severity=severity,
    )


def _fmt_channel_monitored(e: ChannelMonitored) -> ActivityDTO:
    title = e.channel.title if e.channel and e.channel.title else "(未知频道)"
    cid = e.channel.id if e.channel else None
    return ActivityDTO(
        event_type="channel_monitored",
        occurred_at=e.occurred_at,
        icon="kind_channel",
        title="已监听频道",
        detail=title,
        channel_id=cid,
    )


def _fmt_channel_unmonitored(e: ChannelUnmonitored) -> ActivityDTO:
    return ActivityDTO(
        event_type="channel_unmonitored",
        occurred_at=e.occurred_at,
        icon="kind_group",
        title="已取消监听",
        detail=f"频道 #{e.channel_id}",
        channel_id=e.channel_id,
    )


def _fmt_channel_sync_done(e: ChannelSyncDone) -> ActivityDTO:
    result = e.result
    added = getattr(result, "total_messages_added", None)
    detail = f"新增 {added} 条" if added is not None else "同步完成"
    return ActivityDTO(
        event_type="channel_sync_done",
        occurred_at=e.occurred_at,
        icon="nav_channels",
        title="频道同步完成",
        detail=detail,
    )


def _fmt_batch_done(e: BatchDone) -> ActivityDTO:
    failed = e.failed or (1 if e.error else 0)
    return ActivityDTO(
        event_type="batch_done",
        occurred_at=e.occurred_at,
        icon="refresh",
        title=f"批量操作完成: {e.op or '(未命名)'}",
        detail=f"成功 {e.succeeded} / 失败 {failed}",
        severity="error" if failed else "info",
    )


def _fmt_export_done(e: ExportDone) -> ActivityDTO:
    if e.error:
        return ActivityDTO(
            event_type="export_done",
            occurred_at=e.occurred_at,
            icon="export",
            title="导出失败",
            detail=_clip(e.error),
            severity="error",
        )
    out = getattr(e.result, "out_path", None) if e.result else None
    return ActivityDTO(
        event_type="export_done",
        occurred_at=e.occurred_at,
        icon="export",
        title="导出完成",
        detail=_clip(out, 120),
    )


def _fmt_reconcile(e: MediaReconcileFinished) -> ActivityDTO:
    mode = "试运行" if e.dry_run else "已清理"
    return ActivityDTO(
        event_type="media_reconcile_finished",
        occurred_at=e.occurred_at,
        icon="refresh",  # 图标表无 nav_media,复用 refresh
        title="媒体对账完成",
        detail=f"{mode}: 扫描 {e.scanned} / 孤儿 {e.orphans} / 删除 {e.deleted}",
    )


def _fmt_settings_changed(e: SettingsChanged) -> ActivityDTO:
    return ActivityDTO(
        event_type="settings_changed",
        occurred_at=e.occurred_at,
        icon="settings",
        title="设置已变更",
        detail=_clip(e.what, 80),
    )


def _fmt_error(e: ErrorOccurred) -> ActivityDTO:
    return ActivityDTO(
        event_type="error_occurred",
        occurred_at=e.occurred_at,
        icon="refresh",
        title=f"发生错误: {_clip(e.source, 40)}",
        detail=_clip(e.message or e.exception),
        severity="error",
    )


# event type → formatter（type() 精确匹配；AuthErrorOccurred 走 MRO 命中 ErrorOccurred）
_FORMATTERS: dict[type[Event], Callable[[Any], ActivityDTO]] = {
    LoginStateChanged: _fmt_login,
    ChannelMonitored: _fmt_channel_monitored,
    ChannelUnmonitored: _fmt_channel_unmonitored,
    ChannelSyncDone: _fmt_channel_sync_done,
    BatchDone: _fmt_batch_done,
    ExportDone: _fmt_export_done,
    MediaReconcileFinished: _fmt_reconcile,
    SettingsChanged: _fmt_settings_changed,
    ErrorOccurred: _fmt_error,
}


def format_activity(event: Event) -> ActivityDTO | None:
    """把事件归一化为 ActivityDTO；未覆盖事件（含所有 Progress / 消息类）返回 None。

    按 MRO 匹配：AuthErrorOccurred（ErrorOccurred 子类）自动命中 _fmt_error。
    """
    for cls in type(event).__mro__:
        formatter = _FORMATTERS.get(cls)
        if formatter is not None:
            return formatter(event)
        if cls is Event:
            break
    return None


def make_persistence_subscriber(store: ActivityStore):
    """构造一个 `EventBus.subscribe_all` 订阅者，把覆盖事件落库。

    订阅者内部吞掉所有异常（EventBus 契约本就吞），避免落库失败影响主流程；
    落库失败只记 warning。UI 侧另有订阅者负责实时追加，本订阅者只管持久化。
    """

    async def _subscriber(event: Event) -> None:
        activity = format_activity(event)
        if activity is None:
            return
        try:
            await store.append_activity(activity)
        except Exception:  # noqa: BLE001 — 落库失败不阻断事件主流程
            log.warning("append_activity failed for %s", activity.event_type, exc_info=True)

    return _subscriber


__all__ = [
    "format_activity",
    "make_persistence_subscriber",
]
