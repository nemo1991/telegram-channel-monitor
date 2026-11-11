"""白名单管理组件 — `MonitorService` 的频道白名单 + reconfigure 路径。

2026-11-11 v2.0.0 Phase 3-C3:从 `WhitelistMixin` 改为显式组合组件。
组件持 `svc: MonitorService` 引用,状态单一所有权仍在 MonitorService
(`_whitelist` 等),组件只读写 `self._svc.*` — 不再有 mixin MRO 魔法,
mypy 经 `MonitorService` 类型看到全部契约,无需 `type: ignore[attr-defined]`。

`MonitorService` 保留同名 delegate(`set_whitelist` / `add_to_whitelist` /
`remove_from_whitelist` / `subscribed_ids` / `update_backends`)转发到本组件,
公开调用面与测试 seam 不变。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable

from tgmonitor.core.config import Settings
from tgmonitor.core.monitor.downloader import MediaDownloader
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository

if TYPE_CHECKING:
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)


class WhitelistManager:
    """白名单管理 — 5 个 method:`set_whitelist` / `add_to_whitelist` /
    `remove_from_whitelist` / `subscribed_ids` / `update_backends`。

    经 `self._svc` 访问宿主状态:`bus / client / storage / objects /
    settings / downloader / _whitelist`。
    """

    def __init__(self, svc: MonitorService) -> None:
        self._svc = svc

    def set_whitelist(self, channel_ids: Iterable[int]) -> None:
        """替换白名单 — 由 AppService 启动 monitor 时调。"""
        self._svc._whitelist = set(channel_ids)

    def add_to_whitelist(self, channel_id: int) -> None:
        """增量加一个频道到白名单(订阅后立即生效)。"""
        self._svc._whitelist.add(channel_id)

    def remove_from_whitelist(self, channel_id: int) -> None:
        """从白名单摘掉一个频道(退订;不存在 idempotent 不抛)。"""
        self._svc._whitelist.discard(channel_id)

    @property
    def subscribed_ids(self) -> frozenset[int]:
        """订阅频道 id 的快照集合 — UI 只读访问用。

        内部 `_whitelist` 仍由 `set_whitelist` / `add_to_whitelist` /
        `remove_from_whitelist` 维护;每次访问返回一个新 `frozenset` 副本,
        UI 端不会意外修改内部状态。
        """
        return frozenset(self._svc._whitelist)

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
        svc = self._svc
        svc.storage = storage
        svc.objects = objects
        svc.settings = settings
        if svc.downloader is not None:
            svc.downloader = MediaDownloader(
                svc.client,
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
