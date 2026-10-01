# mypy: disable-error-code=attr-defined
# 访问 `MainWindow._on_sync_requested`(复杂 38 行 orchestrator,留 main_window)
# — controller 通过私有成员调用复用,避免重复实现。
"""全量同步 controller — `_on_sync_*` / `_on_refresh_channels` / `_on_sync_all_channels`。

2026-10-01 v1.12.x 从 `MainWindow` 抽出。4 个 handler:
- `_on_refresh_channels`:刷新已订阅频道列表
- `_on_sync_all_channels`:Dashboard 顶部「全量同步」按钮
- `_on_sync_progress`:VM `ChannelSyncProgress` event → 状态栏活动指示器
- `_on_sync_done`:VM `ChannelSyncDone` event → 状态栏汇总提示

`_on_sync_requested` 是复杂 38 行 orchestrator(含 3 个 helpers + async
`_go` + signal cleanup),留 `MainWindow` 处理。`on_sync_all_channels` 通
过 `mw._on_sync_requested(ids)` 复用。
"""

from __future__ import annotations

from tgmonitor.core.events import ChannelSyncDone, ChannelSyncProgress
from tgmonitor.ui.controllers._base import MainWindowCtx


class SyncController:
    """全量同步入口 + 进度反馈。"""

    def __init__(self, ctx: MainWindowCtx) -> None:
        self._ctx = ctx

    def on_refresh_channels(self) -> None:
        mw = self._ctx.main_window
        self._ctx.status_bar.show_message(mw.tr("拉取频道列表…"), 2000)
        self._ctx.status_bar.show_activity("拉取频道列表…")
        self._ctx.vm.refresh_subscribed_channels()

    def on_sync_all_channels(self) -> None:
        """大盘快速操作:全量同步所有已订阅频道。

        委托 `MainWindow._on_sync_requested`(复杂 38 行 orchestrator,含
        3 个 helpers + async `_go()` + signal cleanup)— 后续 PR 再搬。
        """
        from PySide6.QtWidgets import QMessageBox

        mw = self._ctx.main_window
        ids = list(self._ctx.monitor.subscribed_ids)
        if not ids:
            QMessageBox.information(
                mw,
                mw.tr("全量同步"),
                mw.tr("已监听列表为空,先订阅频道"),
            )
            return
        # 复用 MainWindow 上的完整编排器(controller 不重复实现)
        mw._on_sync_requested(ids)

    def on_sync_progress(self, e) -> None:
        """全量同步进度 → 左侧活动指示器持续显示。dialog 自身进度条同步。"""
        if not isinstance(e, ChannelSyncProgress):
            return
        if e.total and e.total > 0:
            self._ctx.status_bar.show_activity(
                f"同步 #{e.channel_id}: {e.done}/{e.total} ({e.stage})"
            )
        else:
            self._ctx.status_bar.show_activity(f"同步 #{e.channel_id}: {e.stage}")

    def on_sync_done(self, e) -> None:
        """全量同步完成 → 短暂显示汇总。"""
        if not isinstance(e, ChannelSyncDone):
            return
        msg = f"同步完成: +{e.new_messages} 条新消息"
        if e.failures:
            msg += f" / {len(e.failures)} 失败"
        self._ctx.status_bar.show_activity(msg, timeout_ms=3000)
