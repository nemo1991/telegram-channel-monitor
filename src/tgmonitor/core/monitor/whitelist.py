"""白名单管理 mixin — `MonitorService` 的频道白名单 + reconfigure 路径。

2026-10-10 v1.12.x plan-batch2 目标 2 从 `MonitorService` 抽提:白名单
管理是低风险分组(无共享热路径状态),抽到独立 mixin 后 `MonitorService`
组合 4 mixin + 主处理逻辑,文件从 1,278 → 700 LOC。

为什么用 mixin 不是组合对象:
- `MonitorService` 内部大量 method 互相调 `_whitelist` / `_seen_ids` /
  `_handled` 等实例状态,改成组合对象要传 `self` 引用,反而繁
- mixin 加 method 不动 `__init__`,`MonitorService.__init__` 一处
  管所有状态初始化
- 4 mixin 文件各自只关心自己方法的逻辑,无重叠

依赖:`Mixin` 期望宿主类持有 `bus: EventBus` / `client: TelegramClient` /
`storage: StorageRepository` / `objects: ObjectStore` /
`settings: Settings` / `downloader: MediaDownloader | None`,
本文件不直接引用 `MonitorService`(避免循环),只列协议契约。
"""

from __future__ import annotations

import logging
from typing import Iterable

from tgmonitor.core.config import Settings
from tgmonitor.core.events import EventBus
from tgmonitor.core.monitor.downloader import MediaDownloader
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient

log = logging.getLogger(__name__)


class WhitelistMixin:
    """白名单管理 — 5 个 method 集中:`set_whitelist` / `add_to_whitelist` /
    `remove_from_whitelist` / `subscribed_ids` / `update_backends`。

    依赖宿主属性:`self.bus / client / storage / objects / settings /
    downloader / _whitelist`。
    """

    bus: EventBus
    client: TelegramClient
    storage: StorageRepository
    objects: ObjectStore
    settings: Settings
    downloader: MediaDownloader | None
    _whitelist: set[int]

    def set_whitelist(self, channel_ids: Iterable[int]) -> None:
        """替换白名单 — 由 AppService 启动 monitor 时调。"""
        self._whitelist = set(channel_ids)

    def add_to_whitelist(self, channel_id: int) -> None:
        """增量加一个频道到白名单(订阅后立即生效)。"""
        self._whitelist.add(channel_id)

    def remove_from_whitelist(self, channel_id: int) -> None:
        """从白名单摘掉一个频道(退订;不存在 idempotent 不抛)。"""
        self._whitelist.discard(channel_id)

    @property
    def subscribed_ids(self) -> frozenset[int]:
        """订阅频道 id 的快照集合 — UI 只读访问用。

        内部 `_whitelist` 仍由 `set_whitelist` / `add_to_whitelist` /
        `remove_from_whitelist` 维护;每次访问返回一个新 `frozenset` 副本,
        UI 端不会意外修改内部状态。
        """
        return frozenset(self._whitelist)

    async def update_backends(
        self,
        storage: StorageRepository,
        objects: ObjectStore,
        settings: Settings,
    ) -> None:
        """热重载后把新 storage/objects/settings 同步给 monitor 及其下载器。

        由 `AppService.reconfigure` 调用(2026-08-18 修):之前 reconfigure
        只换 AppService 自己的引用,monitor / MediaDownloader 仍持旧
        storage —— 热重载切 PG 后实时 / 补拉消息仍写旧库,重启才生效。
        这里同步替换引用 + 重建 MediaDownloader(新 storage/objects/
        max_bytes)+ 从新 storage 重载白名单(订阅真理随存储切换)。
        """
        self.storage = storage
        self.objects = objects
        self.settings = settings
        if self.downloader is not None:
            self.downloader = MediaDownloader(
                self.client,
                storage,
                objects,
                max_bytes=settings.media_max_bytes,
            )
        # 订阅真理随存储切换:白名单从新 storage 重载;失败只降级记日志,
        # 不阻断 reconfigure 的其余提交(保留原白名单,下一次订阅/退订事件会修正)。
        try:
            subscribed = await storage.list_subscribed_channels()
        except Exception:  # noqa: BLE001
            log.exception("update_backends: 从新 storage 重载白名单失败,保持原白名单")
            return
        self.set_whitelist(c.id for c in subscribed)
