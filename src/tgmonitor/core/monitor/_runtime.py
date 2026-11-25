"""MonitorRuntime — MonitorService 与 5 个组件共享的依赖 + 状态容器。

2026-11-25 P2-M4:打破"组件反向持有宿主 svc"的双向耦合。原先 5 个组件
各自 `self._svc: MonitorService` 读写宿主私有状态(78 处 `self._svc.*`),
宿主又 delegate 回组件 —— 状态所有权名义集中、实际分散,且组件与 24KB
的 MonitorService 类强耦合。

现在依赖 + 状态集中在本对象:
- `MonitorService` 持 `self._rt`,公开属性 / 测试 seam 以 property 转发,
  调用面与 seam 位置完全不变;
- 5 个组件只持 `self._rt`,不再 import / 依赖 `MonitorService`;
- 跨组件调用(handle / backfill_all / delete_with_orphan_check)走 rt 上的
  **晚绑定 callback 槽**(由 MonitorService 构造时注入 `lambda: self._xxx()`),
  这样 `monitor._backfill_all = stub` 等测试 patch 仍能被组件看到。

本模块零 Qt / 零 Telethon 具体类型泄漏:只依赖 core 内的 ABC / DTO / EventBus。
"""

from __future__ import annotations

import asyncio
import collections
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from tgmonitor.core.config import Settings
from tgmonitor.core.dto import MessageDTO
from tgmonitor.core.events import EventBus
from tgmonitor.core.monitor.downloader import MediaDownloader
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream

# 晚绑定回调:签名各异,统一用 Callable[..., Coroutine](可直传 create_task)。
_Callback = Callable[..., Coroutine[Any, Any, Any]]


@dataclass
class MonitorRuntime:
    """共享依赖 + 可变状态 + 跨组件回调槽。"""

    # ---- 依赖(reconfigure 时原地替换) ----
    bus: EventBus
    client: TelegramClient
    storage: StorageRepository
    objects: ObjectStore
    settings: Settings
    downloader: MediaDownloader | None

    # ---- 只读 / 可调配置(测试可 patch) ----
    heartbeat_interval: float
    backfill_interval: float
    backfill_limit: int
    backfill_max_page: int

    # ---- 可变状态 ----
    whitelist: set[int] = field(default_factory=set)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    seen_ids: collections.OrderedDict[tuple[int, int], None] = field(
        default_factory=collections.OrderedDict
    )
    handled: int = 0
    last_heartbeat: float = 0.0
    skip_next_tick: bool = False
    unavailable_channels: set[int] = field(default_factory=set)
    stream: UpdateStream | None = None
    task: asyncio.Task[None] | None = None
    backfill_task: asyncio.Task[None] | None = None
    download_task: asyncio.Task[None] | None = None
    download_queue: asyncio.Queue[tuple[MessageDTO, int]] | None = None

    # ---- 跨组件晚绑定回调(由 MonitorService.__init__ 注入) ----
    # 用 lambda 解析宿主当前属性,使 `monitor._backfill_all = stub` 等 patch 生效。
    handle_message: _Callback | None = None
    handle_edited: _Callback | None = None
    backfill_all: _Callback | None = None
    download_worker: _Callback | None = None
    delete_with_orphan_check: _Callback | None = None
    handle_interactions_changed: _Callback | None = None
    handle_pin_changed: _Callback | None = None
    handle_message_deleted: _Callback | None = None
    handle_channel_metadata: _Callback | None = None
    handle_channel_title_changed: _Callback | None = None
    handle_channel_photo_changed: _Callback | None = None
    handle_connection_state: _Callback | None = None
    backfill_loop: _Callback | None = None
