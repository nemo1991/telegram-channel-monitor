# mypy: disable-error-code=attr-defined
"""频道列表刷新 controller(原名同步反馈 controller)。

2026-10-01 v1.12.x 从 `MainWindow` 抽出;2026-11-25 v2.1.3 P0-C1 收窄为单一
`on_refresh_channels`:
- `_on_refresh_channels`:刷新已监听列表(refresh_button 仍在使用)

2026-11-19:桌面端「全量同步」按钮整体移除,`on_sync_all_channels` 与
`SyncFlowController` / `sync_dialog` 一并删除。core `channel_sync` 能力保留
(CLI `tgmon-cli sync` 仍可用);其 progress/done 事件由 CLI 自己消费、由
`DashboardWidget` 直接订阅 bus 经 `format_activity` 渲染活动账本。

已删的 `on_sync_progress` / `on_sync_done` 读的是事件上不存在的字段
(`ChannelSyncProgress` 字段为 progress/total/detail,`ChannelSyncDone` 只有
result),且 GUI 侧无事件源 —— 死代码清理。
"""

from __future__ import annotations

from tgmonitor.ui.controllers._base import MainWindowCtx


class SyncController:
    """已监听频道列表刷新。"""

    def __init__(self, ctx: MainWindowCtx) -> None:
        self._ctx = ctx

    def on_refresh_channels(self) -> None:
        mw = self._ctx.main_window
        self._ctx.status_bar.show_message(mw.tr("拉取频道列表…"), 2000)
        self._ctx.status_bar.show_activity("拉取频道列表…")
        self._ctx.vm.refresh_monitored_channels()
