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
from collections.abc import Callable
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
from tgmonitor.plugins.config import (
    PluginConfig,
    read_all_config,
    read_enabled,
    write_all_config,
    write_enabled,
)
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
        # 阶段 4 插件管理页:发现缓存 + 激活依赖快照 + 状态订阅。
        # 热 enable 需要重建 PluginContext(bus / 域服务 / settings),这些在
        # bootstrap 时才有;activate_all 成功后留快照,运行期 enable 复用。
        self._discovered: dict[str, DiscoveredPlugin] = {}
        self._bus: EventBus | None = None
        self._app_services: dict[str, Any] = {}
        self._settings_view: SettingsView | None = None
        self._state_listeners: list[Callable[[], None]] = []

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
        self._discovered = {item.manifest.id: item for item in discovered}
        # 留激活依赖快照 —— 运行期 enable 要用同一 bus / 域服务 / settings
        # 重建 PluginContext,不能让热启用拿到一个空门面。
        self._bus = bus
        self._app_services = dict(app_services)
        self._settings_view = SettingsView.from_settings(settings)

        enabled = read_enabled(self._plugins_dir)
        activated: list[PluginRecord] = []

        for item in discovered:
            pid = item.manifest.id
            if pid not in enabled:
                self._records[pid] = PluginRecord(
                    manifest=item.manifest, state=PluginState.DISABLED, source=item.source
                )
                continue
            rec = self._activate_one(item)
            self._records[pid] = rec
            if rec.state is PluginState.ACTIVE:
                activated.append(rec)
        return activated

    def _activate_one(self, item: DiscoveredPlugin) -> PluginRecord:
        """激活单个插件 — activate_all 与热 enable 共用的唯一激活路径。

        版本门禁 / 未知能力警告 / activate / exporter 注册 / ui 贡献读取都
        收在这里,保证启动激活与运行期热启用行为完全一致。失败返回 FAILED
        记录(不写 `_records`,由调用方决定何时落账)。
        """
        pid = item.manifest.id
        if not item.manifest.is_compatible(self._api_version):
            log.warning("插件 %s 版本不兼容,跳过", pid)
            return PluginRecord(
                manifest=item.manifest,
                state=PluginState.FAILED,
                source=item.source,
                error=(
                    f"需要插件 API >= {item.manifest.min_app_version}, 当前 {self._api_version}"
                ),
            )
        for cap in item.manifest.unknown_capabilities():
            log.warning("插件 %s 声明了未知能力 %s,宿主将忽略", pid, cap)

        assert self._bus is not None and self._settings_view is not None
        ctx = self._build_context(item, self._bus, self._app_services, self._settings_view)
        # exporter 注册在 ui 读取之前 —— 两者之间失败时必须回滚已注册格式,
        # 否则会留下「插件 FAILED 但导出格式还活着」的幽灵能力。
        export_formats: tuple[str, ...] = ()
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
            for fmt in export_formats:
                try:
                    EXPORTERS.unregister(fmt)
                except Exception:
                    log.exception("插件 %s 回滚导出格式 %s 失败", pid, fmt)
            ctx.deactivate_hooks()  # 激活中途可能已订阅,失败时回滚订阅
            return PluginRecord(
                manifest=item.manifest,
                state=PluginState.FAILED,
                source=item.source,
                error=str(exc),
            )
        return PluginRecord(
            manifest=item.manifest,
            state=PluginState.ACTIVE,
            source=item.source,
            plugin=plugin,
            context=ctx,
            export_formats=export_formats,
            ui_contribution=ui_contribution,
        )

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

    # ------------------------------------------------------------------
    # 热切换 API(阶段 4 插件管理页)
    # ------------------------------------------------------------------

    def subscribe_state(self, listener: Callable[[], None]) -> Callable[[], None]:
        """订阅插件状态变更 — UI 管理页据此刷新列表/导航页。

        返回退订函数(幂等),供 widget 析构时解绑,避免单例持有已销毁视图。
        通知失败只记日志,不影响 enable/disable 主流程。
        """
        if listener not in self._state_listeners:
            self._state_listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._state_listeners:
                self._state_listeners.remove(listener)

        return unsubscribe

    def _notify_state(self) -> None:
        for listener in list(self._state_listeners):
            try:
                listener()
            except Exception:
                log.exception("插件状态变更通知失败,已隔离")

    def refresh_discovered(self) -> tuple[str, ...]:
        """重跑双通道发现,把新出现的插件登记进记录表。

        只登记新插件为 DISABLED(默认安全),**不改动已有插件的状态**——
        否则热切换后的 ACTIVE/DISABLED 会被发现流程冲掉。已卸载插件的旧
        记录保留(状态仍是 DISABLED/FAILED),避免误删用户配置命名空间;
        `enable` 时会对不在 `_discovered` 的 id 拒绝。返回新发现的 id。
        """
        found = discover_all(self._plugins_dir)
        self._discovered = {item.manifest.id: item for item in found}
        fresh: list[str] = []
        for pid, item in self._discovered.items():
            if pid not in self._records:
                self._records[pid] = PluginRecord(
                    manifest=item.manifest, state=PluginState.DISABLED, source=item.source
                )
                fresh.append(pid)
        if fresh:
            self._notify_state()
        return tuple(fresh)

    def enable(self, plugin_id: str) -> bool:
        """热启用一个已发现插件 — 即时生效,无需重启。

        与 `activate_all` 走同一条 `_activate_one` 路径:版本门禁 / activate /
        exporter 注册 / ui 贡献读取完全一致。成功后:
        - 写 `enabled.json`(持久化启用意图,重启仍生效);
        - 若宿主已处于运行期(`_running`)则立即调该插件 `start()`;
        - 通知状态订阅者刷新 UI。
        失败返回 False 并标 FAILED,不改其它插件。重复启用 ACTIVE 插件幂等。
        """
        if plugin_id not in self._discovered:
            log.warning("插件 %s 未发现,无法启用", plugin_id)
            return False
        rec = self._records.get(plugin_id)
        if rec is not None and rec.state is PluginState.ACTIVE:
            return True

        activated = self._activate_one(self._discovered[plugin_id])
        self._records[plugin_id] = activated
        if activated.state is not PluginState.ACTIVE:
            self._notify_state()
            return False

        try:
            write_enabled(
                self._plugins_dir,
                frozenset(set(read_enabled(self._plugins_dir)) | {plugin_id}),
            )
        except Exception:
            log.exception("插件 %s 启用清单写盘失败", plugin_id)

        if self._running and activated.plugin is not None:
            hook = getattr(activated.plugin, "start", None)
            if hook is not None:
                try:
                    hook()
                except Exception:
                    log.exception("插件 %s 热启用 start 失败,已隔离", plugin_id)
        self._notify_state()
        return True

    def disable(self, plugin_id: str) -> bool:
        """热停用插件 — 即时撤销能力,无需重启。

        顺序:start() 的逆操作 → 注销 exporter → 退订事件 → deactivate()
        → 从 `enabled.json` 移除 → 标 DISABLED → 通知 UI。每步失败只记日志,
        保证停用总能走完(与 deactivate_all 同策略)。重复停用幂等。
        """
        rec = self._records.get(plugin_id)
        if rec is None or rec.state is PluginState.DISABLED:
            return True

        if self._running and rec.plugin is not None:
            hook = getattr(rec.plugin, "stop", None)
            if hook is not None:
                try:
                    hook()
                except Exception:
                    log.exception("插件 %s 热停用 stop 失败,已隔离", plugin_id)
        for fmt in rec.export_formats:
            try:
                EXPORTERS.unregister(fmt)
            except Exception:
                log.exception("插件 %s 注销导出格式 %s 失败", plugin_id, fmt)
        if rec.context is not None:
            rec.context.deactivate_hooks()
        if rec.plugin is not None:
            try:
                rec.plugin.deactivate()
            except Exception:
                log.exception("插件 %s deactivate 失败", plugin_id)

        try:
            write_enabled(
                self._plugins_dir,
                frozenset(set(read_enabled(self._plugins_dir)) - {plugin_id}),
            )
        except Exception:
            log.exception("插件 %s 启用清单写盘失败", plugin_id)

        self._records[plugin_id] = PluginRecord(
            manifest=rec.manifest, state=PluginState.DISABLED, source=rec.source
        )
        self._notify_state()
        return True

    def config_for(self, plugin_id: str) -> dict[str, Any]:
        """读某插件配置命名空间 — UI 管理页读值用。"""
        return read_all_config(self._plugins_dir).get(plugin_id, {})

    def save_config(self, plugin_id: str, data: dict[str, Any]) -> None:
        """覆盖写某插件配置命名空间 — 只动自己 id 下的子树,其它插件不变。

        KV 表格的 JSON 字面量解析在 UI 层完成(保留数字/布尔/数组类型),
        宿主只负责原子写盘。写失败上抛,由对话框给用户提示。
        """
        all_config = read_all_config(self._plugins_dir)
        all_config[plugin_id] = data
        write_all_config(self._plugins_dir, all_config)


# 进程级单例 — GUI / CLI 共用,复用 EXPORTERS 范式。
PLUGIN_HOST = PluginHost()
