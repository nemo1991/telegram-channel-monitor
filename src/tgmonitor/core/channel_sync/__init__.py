"""ChannelSyncService — 用户多选频道后的全量同步(元数据 + 历史消息)。

触发:`AppService.sync_channels(channel_ids, options)` 由 UI "全量同步…"
按钮经进度对话框调用。

详见 `service.py`。
"""
from tgmonitor.core.channel_sync.service import ChannelSyncService

__all__ = ["ChannelSyncService"]
