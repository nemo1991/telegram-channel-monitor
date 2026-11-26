"""插件宿主 — 状态机 + 进程级单例 + 生命周期钩子。

`PluginHost` 可独立实例化(测试友好),`PLUGIN_HOST` 是进程级单例
(复用 `core.export.base.EXPORTERS` 的既有范式)。宿主不进入 `AppService`
的公开表面 — 它由 `core.runtime.bootstrap()` 在装配完成后激活,
在 `shutdown()` 里先于 backend 关闭而停用。

**失败隔离**:单个插件的发现 / 实例化 / `activate` 任一阶段抛异常,
都只标记该插件 FAILED 并记日志,不向上抛、不阻断启动。这与
objectstore.connect 的降级策略一致。

**发现 ≠ 启用**:manifest 只登记,只有 `enabled.json` 显式列出的 id 才激活。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from tgmonitor.core.config import _user_data_dir
from tgmonitor.core.events import EventBus
from tgmonitor.plugins.api import (
    EventBusView,
    Plugin,
    PluginContext,
    SettingsView,
    _DelegatingPluginApi,
)
from tgmonitor.plugins.config import PluginConfig, read_all_config, read_enabled
from tgmonitor.plugins.loader import DiscoveredPlugin, discover_all
from tgmonitor.plugins.manifest import PluginManifest

log = logging.getLogger(__name__)

# 插件系统的契约版本 — 与 app 营销版本号解耦。插件的 min_app_version 与
# 这个比,而不是 `__version__`(2.1.7):这样 3.0.0 发版前就能加载插件开发,
# 且发版时的 6 处 bump 不需要动插件系统。
PLUGIN_API_VERSION = "3.0.0"


class PluginState(str, Enum):
    """插件生命周期状态。"""

    DISCOVERED = "discovered"  # 已发现,但未启用 / 版本不兼容
    ACTIVE = "active"
    FAILED = "failed"  # 激活失败,已隔离
    DISABLED = "disabled"  # 显式停用(发现过但未在 enabled.json)


@dataclass
class PluginRecord:
    """单个插件的运行时记录 — 诊断 / UI 插件管理页读取。"""

    manifest: PluginManifest
    state: PluginState
    source: str
    error: str | None = None
    plugin: Plugin | None = field(default=None, repr=False)
    context: PluginContext | None = field(default=None, repr=False)


class PluginHost:
    """插件宿主:发现 → 门禁 → 激活 → 停用。可独立实例化供测试。"""

    def __init__(
        self,
        plugins_dir: Path | None = None,
        *,
        api_version: str = PLUGIN_API_VERSION,
    ) -> None:
        self._plugins_dir = plugins_dir or (_user_data_dir() / "plugins")
        self._api_version = api_version
        self._records: dict[str, PluginRecord] = {}
        self._activated = False

    @property
    def plugins_dir(self) -> Path:
        return self._plugins_dir

    def records(self) -> Mapping[str, PluginRecord]:
        """只读记录快照 — UI 管理页 / 诊断用。"""
        return MappingProxyType(dict(self._records))

    def activate_all(
        self,
        bus: EventBus,
        *,
        app_services: Mapping[str, Any],
        settings: Any,
    ) -> list[PluginRecord]:
        """发现并激活所有已启用插件。

        `app_services` 是 `{"messages": ..., "media": ..., "channels": ...}`
        的域服务映射 — 由调用方(runtime)传入,host 不直接依赖 AppService,
        避免形成新的 god 依赖。返回值是本轮被激活的记录列表(诊断用)。
        """
        if self._activated:
            log.debug("PluginHost 已激活,跳过重复 activate_all")
            return []
        self._activated = True

        discovered = discover_all(self._plugins_dir)
        enabled = read_enabled(self._plugins_dir)
        settings_view = SettingsView.from_settings(settings)
        activated: list[PluginRecord] = []

        for item in discovered:
            pid = item.manifest.id
            if pid not in enabled:
                self._records[pid] = PluginRecord(
                    manifest=item.manifest, state=PluginState.DISABLED, source=item.source
                )
                continue
            if not item.manifest.is_compatible(self._api_version):
                self._records[pid] = PluginRecord(
                    manifest=item.manifest,
                    state=PluginState.FAILED,
                    source=item.source,
                    error=(
                        f"需要插件 API >= {item.manifest.min_app_version}, 当前 {self._api_version}"
                    ),
                )
                log.warning("插件 %s 版本不兼容,跳过", pid)
                continue
            for cap in item.manifest.unknown_capabilities():
                log.warning("插件 %s 声明了未知能力 %s,宿主将忽略", pid, cap)

            ctx = self._build_context(item, bus, app_services, settings_view)
            try:
                plugin = item.factory()
                plugin.activate(ctx)
            except Exception as exc:
                log.exception("插件 %s activate 失败,已隔离", pid)
                ctx.deactivate_hooks()  # 激活中途可能已订阅,失败时回滚订阅
                self._records[pid] = PluginRecord(
                    manifest=item.manifest,
                    state=PluginState.FAILED,
                    source=item.source,
                    error=str(exc),
                )
            else:
                self._records[pid] = PluginRecord(
                    manifest=item.manifest,
                    state=PluginState.ACTIVE,
                    source=item.source,
                    plugin=plugin,
                    context=ctx,
                )
                activated.append(self._records[pid])
        return activated

    def _build_context(
        self,
        item: DiscoveredPlugin,
        bus: EventBus,
        app_services: Mapping[str, Any],
        settings_view: SettingsView,
    ) -> PluginContext:
        """为每个插件构造独立上下文 — 独立 EventBusView(订阅记账隔离)。"""
        return PluginContext(
            bus=EventBusView(bus, owner=item.manifest.id),
            api=_DelegatingPluginApi(app_services),
            config=PluginConfig(self._plugins_dir, item.manifest.id),
            settings=settings_view,
            manifest=item.manifest,
        )

    def deactivate_all(self) -> None:
        """停用全部插件:先退订事件,再调插件 deactivate。幂等。

        失败只记日志不抛,保证 shutdown 总能走完(与 runtime.shutdown 策略一致)。
        """
        for pid, rec in self._records.items():
            if rec.context is not None:
                rec.context.deactivate_hooks()
            if rec.plugin is not None:
                try:
                    rec.plugin.deactivate()
                except Exception:
                    log.exception("插件 %s deactivate 失败", pid)
        self._activated = False

    def config_for(self, plugin_id: str) -> dict[str, Any]:
        """读某插件配置命名空间 — UI 管理页读值用。"""
        return read_all_config(self._plugins_dir).get(plugin_id, {})


# 进程级单例 — GUI / CLI 共用,复用 EXPORTERS 范式。
PLUGIN_HOST = PluginHost()
