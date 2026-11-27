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
from tgmonitor.core.export.base import EXPORTERS
from tgmonitor.plugins.api import (
    EventBusView,
    Plugin,
    PluginContext,
    SettingsView,
    UiContribution,
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
    # 本插件注册的导出格式 key — host 记账,deactivate 时逐个 unregister。
    export_formats: tuple[str, ...] = ()
    # UI 页面贡献(阶段 4)— 只含数据 + factory,不含 QWidget/QIcon。
    ui_contribution: UiContribution | None = None


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
        # 运行期钩子状态 — 与 _activated(装配期)分离。start_all/stop_all 幂等,
        # 由 AppService.start/shutdown + pause/resume 驱动,可反复切换。
        self._running = False

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
                # export 能力:activate 成功后才注册 —— 插件可能按 config
                # 决定提供哪些格式,且激活失败时不应留下残留格式。
                export_formats = self._register_exporters(plugin, item.manifest)
                # ui 能力:activate 成功后才读贡献 —— 插件可能按 config 决定
                # 是否贡献页面。只取数据 + factory,不构造任何 Qt 对象。
                ui_contribution = self._read_ui_contribution(plugin, item.manifest)
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
                    export_formats=export_formats,
                    ui_contribution=ui_contribution,
                )
                activated.append(self._records[pid])
        return activated

    def _register_exporters(self, plugin: Plugin, manifest: PluginManifest) -> tuple[str, ...]:
        """把插件声明的 exporter 注册到全局注册表,返回注册到的 format key。

        失败时回滚已注册的格式再上抛 —— 由 `activate_all` 的 except 收口
        将该插件标记 FAILED。注册原语在 registry,生命周期驱动在 host。
        """
        if "export" not in manifest.capabilities:
            return ()
        providers = getattr(plugin, "exporters", ())
        if not providers:
            log.warning("插件 %s 声明 export 能力但未提供 exporters", manifest.id)
            return ()
        registered: list[str] = []
        try:
            for provider in providers:
                instance = provider() if isinstance(provider, type) else provider
                registered.append(EXPORTERS.register(instance))
        except Exception:
            for fmt in registered:
                EXPORTERS.unregister(fmt)
            raise
        log.info("插件 %s 注册导出格式: %s", manifest.id, registered)
        return tuple(registered)

    def _read_ui_contribution(
        self, plugin: Plugin, manifest: PluginManifest
    ) -> UiContribution | None:
        """读插件声明的 UI 页面贡献(不做任何 Qt 构造)。

        只在 manifest 声明 ui 能力时读;属性缺失 / 返回 None(插件按配置决定
        不贡献)都返回 None。属性 getter 抛错向上抛 —— 由 `activate_all` 的
        except 收口把插件标记 FAILED(与 exporter 注册失败同策略)。
        """
        if "ui" not in manifest.capabilities:
            return None
        raw = getattr(plugin, "ui_contribution", None)
        if raw is None:
            return None
        if not isinstance(raw, UiContribution):
            log.warning("插件 %s 的 ui_contribution 类型不符,忽略", manifest.id)
            return None
        return raw

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

    def start_all(self) -> None:
        """会话就绪后启动全部 ACTIVE 插件的运行期钩子。幂等、失败隔离。

        只对已 `activate` 成功(ACTIVE)的插件调可选 `start()`;未实现该钩子的
        老插件视为 no-op。重复调用只生效一次(`_running` 守卫)—— 对应
        app.start() 与 resume_monitor() 可能都走到「就绪」的场景。
        单个插件 start 抛异常只记日志,不影响其它插件,也不改它的 ACTIVE 态
        (启动失败可由插件自己在日志里定位,宿主不做半启动回滚)。
        """
        if not self._activated or self._running:
            return
        self._running = True
        for pid, rec in self._records.items():
            if rec.state is not PluginState.ACTIVE or rec.plugin is None:
                continue
            hook = getattr(rec.plugin, "start", None)
            if hook is None:
                continue
            try:
                hook()
            except Exception:
                log.exception("插件 %s start 失败,已隔离", pid)

    def stop_all(self) -> None:
        """停止全部插件的运行期钩子。幂等、失败隔离。

        只作用于曾成功 `start_all` 的插件(`_running` 守卫),未 start 时 no-op。
        单个插件 stop 抛异常只记日志,保证 pause / shutdown 总能走完。
        """
        if not self._running:
            return
        self._running = False
        for pid, rec in self._records.items():
            if rec.state is not PluginState.ACTIVE or rec.plugin is None:
                continue
            hook = getattr(rec.plugin, "stop", None)
            if hook is None:
                continue
            try:
                hook()
            except Exception:
                log.exception("插件 %s stop 失败", pid)

    def deactivate_all(self) -> None:
        """停用全部插件:注销导出格式 → 退订事件 → 调插件 deactivate。幂等。

        失败只记日志不抛,保证 shutdown 总能走完(与 runtime.shutdown 策略一致)。
        进入装配期拆除前先 `stop_all()` —— shutdown 路径可能没经过 AppService
        (如测试直接调 runtime.shutdown),确保运行期钩子先停下再撤上下文。
        """
        self.stop_all()
        for pid, rec in self._records.items():
            for fmt in rec.export_formats:
                try:
                    EXPORTERS.unregister(fmt)
                except Exception:
                    log.exception("插件 %s 注销导出格式 %s 失败", pid, fmt)
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
