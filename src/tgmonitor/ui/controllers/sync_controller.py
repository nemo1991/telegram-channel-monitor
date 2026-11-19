# mypy: disable-error-code=attr-defined
# 访问 `MainWindow._on_sync_requested`(复杂 38 行 orchestrator,留 main_window)
# — controller 通过私有成员调用复用,避免重复实现。
"""同步反馈 controller — `_on_refresh_channels` / `_on_sync_progress` / `_on_sync_done`。

2026-10-01 v1.12.x 从 `MainWindow` 抽出。3 个 handler:
- `_on_refresh_channels`:刷新已监听列表
- `_on_sync_progress`:VM `ChannelSyncProgress` event → 状态栏活动指示器
- `_on_sync_done`:VM `ChannelSyncDone` event → 状态栏汇总提示

2026-11-19:桌面端「全量同步」按钮整体移除，`on_sync_all_channels` 与
`SyncFlowController` / `sync_dialog` 一并删除。core `channel_sync` 能力保留
（CLI `tgmonitor sync` 仍可用），其产生的 progress/done 事件仍由本 controller
反馈到状态栏。
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
        self._ctx.vm.refresh_monitored_channels()

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
