"""插件系统 — 3.0.0 能力插件基础设施。

顶层包(与 core / cli / ui 平级),刻意避开 `tests/test_architecture.py` 对
core/cli 的 AST 扫描。本包是**无状态协议层 + 进程级 host**:

- `manifest`:插件清单(身份 / 版本 / 能力声明)
- `config`:`enabled.json` + `config.json` 原子写 + 命名空间隔离
- `api`:`PluginContext` 窄门面(EventBusView / PluginApi / SettingsView)
- `loader`:双通道发现(entry-points + 本地目录)
- `host`:`PluginHost` 状态机 + 进程级单例 `PLUGIN_HOST`

**边界守则**:本包顶层不 import PySide6 / qasync / `tgmonitor.ui.*`,
CLI 加载本包不得触发任何 UI 框架。插件实例由第三方包提供,宿主只通过
`Plugin` Protocol 调用,跨边界载荷一律 DTO。详见 `docs/ADR-001-plugin-system.md`。
"""

from tgmonitor.plugins.api import (
    EventBusView,
    Plugin,
    PluginApi,
    PluginContext,
    SettingsView,
    UiContribution,
)
from tgmonitor.plugins.host import PLUGIN_HOST, PluginHost
from tgmonitor.plugins.manifest import PluginManifest

__all__ = [
    "PLUGIN_HOST",
    "EventBusView",
    "Plugin",
    "PluginApi",
    "PluginContext",
    "PluginHost",
    "PluginManifest",
    "SettingsView",
    "UiContribution",
]
