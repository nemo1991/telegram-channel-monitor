"""白名单管理组件 — `MonitorService` 的频道白名单 + reconfigure 路径。

2026-11-11 v2.0.0 Phase 3-C3:从 `WhitelistMixin` 改为显式组合组件。
2026-11-25 P2-M4:组件改为持有共享 `MonitorRuntime`(`self._rt`),不再反向
持有 `MonitorService` —— 打破宿主↔组件双向耦合。状态在 runtime 单一所有权,
`MonitorService` 的同名 delegate / property 转发到本组件与 runtime,公开调用面
与测试 seam 不变。
"""

from __future__ import annotations

import logging
from typing import Iterable

from tgmonitor.core.config import Settings
from tgmonitor.core.monitor._runtime import MonitorRuntime
from tgmonitor.core.monitor.downloader import MediaDownloader
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository

log = logging.getLogger(__name__)


class WhitelistManager:
    """白名单管理 — 5 个 method:`set_whitelist` / `add_to_whitelist` /
    `remove_from_whitelist` / `monitored_ids` / `update_backends`。

    经 `self._rt` 访问共享状态:`whitelist / storage / objects /
    settings / downloader / client`。
    """

    def __init__(self, rt: MonitorRuntime) -> None:
        self._rt = rt

    def set_whitelist(self, channel_ids: Iterable[int]) -> None:
        """替换白名单 — 由 AppService 启动 monitor 时调。"""
        self._rt.whitelist = set(channel_ids)

    def add_to_whitelist(self, channel_id: int) -> None:
        """增量加一个频道到白名单(订阅后立即生效)。"""
        self._rt.whitelist.add(channel_id)

    def remove_from_whitelist(self, channel_id: int) -> None:
        """从白名单摘掉一个频道(退订;不存在 idempotent 不抛)。"""
        self._rt.whitelist.discard(channel_id)

    @property
    def monitored_ids(self) -> frozenset[int]:
        """订阅频道 id 的快照集合 — UI 只读访问用。

        内部 `rt.whitelist` 仍由 `set_whitelist` / `add_to_whitelist` /
        `remove_from_whitelist` 维护;每次访问返回一个新 `frozenset` 副本,
        UI 端不会意外修改内部状态。
        """
        return frozenset(self._rt.whitelist)

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
        rt = self._rt
        rt.storage = storage
        rt.objects = objects
        rt.settings = settings
        if rt.downloader is not None:
            rt.downloader = MediaDownloader(
                rt.client,
                storage,
                objects,
                max_bytes=settings.media_max_bytes,
            )
        # 订阅真理随存储切换:白名单从新 storage 重载;失败只降级记日志,
        # 不阻断 reconfigure 的其余提交(保留原白名单,下一次订阅/退订事件会修正)。
        try:
            subscribed = await storage.list_monitored_channels()
        except Exception:
            log.exception("update_backends: 从新 storage 重载白名单失败,保持原白名单")
            return
        self.set_whitelist(c.id for c in subscribed)
