"""PluginApi / PluginContext allowlist 守护 — 3.0.0 阶段 1。

门面一旦腐烂成第二个 god facade,插件就能绕过域服务直接操作后端。
本测试把 `PluginApi` 的方法集合锁死:新增方法必须显式改 allowlist 并在
commit message 说明,与 `test_app_service_surface.py` 同一治理思路。

同时守护 `PluginContext` 的字段集合 —— 它是对插件暴露的完整能力面,
多一个字段就多一条越权通道(尤其是不能出现裸 bus / client / app)。
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from tgmonitor.core.events import EventBus
from tgmonitor.plugins.api import EventBusView, PluginApi, PluginContext, SettingsView
from tgmonitor.plugins.manifest import PluginManifest

ALLOWED_API_METHODS = {
    "list_messages",
    "list_media",
    "list_monitored_channels",
    "set_favorite",
    "set_tags",
    "set_notes",
    "delete_message",
    "retry_media",
    "download_media",
}

ALLOWED_CONTEXT_FIELDS = {"bus", "api", "config", "settings", "manifest"}


def test_plugin_api_methods_allowlist() -> None:
    actual = {
        name
        for name, member in inspect.getmembers(PluginApi)
        if not name.startswith("_") and callable(member)
    }
    assert actual == ALLOWED_API_METHODS, (
        f"PluginApi 方法表面变更!\n"
        f"  新增: {sorted(actual - ALLOWED_API_METHODS)}\n"
        f"  移除: {sorted(ALLOWED_API_METHODS - actual)}"
    )


def test_plugin_context_fields_allowlist() -> None:
    ctx = PluginContext(
        bus=EventBusView(EventBus()),
        api=MagicMock(),  # type: ignore[arg-type]
        config=MagicMock(),
        settings=SettingsView.from_settings(MagicMock()),
        manifest=PluginManifest(id="x", name="x"),
    )
    actual = set(vars(ctx))
    assert actual == ALLOWED_CONTEXT_FIELDS, (
        f"PluginContext 字段表面变更!\n"
        f"  新增: {sorted(actual - ALLOWED_CONTEXT_FIELDS)}\n"
        f"  移除: {sorted(ALLOWED_CONTEXT_FIELDS - actual)}"
    )


def test_plugin_context_has_no_raw_bus_or_client() -> None:
    """上下文不得直接持有裸 EventBus / TelegramClient / AppService。"""
    fields = set(PluginContext.__dataclass_fields__)
    forbidden = {"client", "app", "app_service", "storage", "objects"}
    assert not (fields & forbidden), f"PluginContext 暴露了禁止字段: {fields & forbidden}"


def test_api_duck_type_matches_protocol() -> None:
    """_DelegatingPluginApi 必须实现 PluginApi 声明的全部方法。"""
    from tgmonitor.plugins.api import _DelegatingPluginApi

    missing = [m for m in ALLOWED_API_METHODS if not hasattr(_DelegatingPluginApi, m)]
    assert missing == [], f"_DelegatingPluginApi 缺方法: {missing}"
