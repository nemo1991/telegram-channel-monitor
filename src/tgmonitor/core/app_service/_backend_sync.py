"""AppService 后端 swap 同步辅助。

2026-11-11 v2.0.0 Phase 3:域服务扩为 channels / messages / media,
storage / objects / downloader 三个 property setter 的传播集中于此。

设计:
- plain module-level function(非 class)
- 第一参数 `app: AppService`(走 `app._xxx` 引用,避免 stale 副本)
- objects=None → 早返(aclose 后置 None 的历史路径);storage 非 None

不在本模块范围:
- `_rebuild_storage` / `_rebuild_objects`(实质 swap 流程)— 在 `_reconfigure.py`
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.monitor.downloader import MediaDownloader
    from tgmonitor.core.monitor.service import MonitorService


def set_monitor(app: AppService, value: MonitorService | None) -> None:
    """swap monitor 时同步给 ChannelService(白名单)/ MessageService(单删)/
    BatchService(批删)。

    触发场景:测试 `app.monitor = AsyncMock()`(构造后注入)。
    """
    app._monitor = value
    app.channels._monitor = value
    app.messages._monitor = value
    app.batch._monitor = value


def set_objects(app: AppService, value: ObjectStore | None) -> None:
    """swap objectstore 时同步给 MediaService。

    `value=None` 是历史兼容路径(`aclose()` 后置 None)— 不写回。
    """
    if value is None:
        return
    app._objects = value
    app.media._objects = value


def set_storage(app: AppService, value: StorageRepository) -> None:
    """swap storage 时同步给 ChannelService + MessageService + MediaService。"""
    app._storage = value
    app.channels._storage = value
    app.messages._storage = value
    app.media._storage = value


def set_downloader(app: AppService, value: MediaDownloader | None) -> None:
    """swap downloader 时同步给 MediaService(retry)+ ChannelSyncService(sync 下载)。

    触发场景:
    - 测试 `app.downloader = stub`
    - reconfigure 末尾 `app.downloader = monitor.downloader`(monitor 重建后)
    """
    app._downloader = value
    app.media._downloader = value
    app.channel_sync.downloader = value
