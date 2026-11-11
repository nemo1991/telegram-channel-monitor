"""AppService 公开表面 allowlist 守护 — 2026-11-11 v2.0.0 Phase 3-C2。

C2 删除 41 个 god-facade 域转发后,本测试锁死组合根最终表面:
新增任何公开方法 / 属性(尤其是域转发)都会让这里失败 —
域操作应加在域服务(auth/channels/messages/media/batch/channel_sync)上,
组合根只留生命周期 / 热重载 / 导出 / 后端持有。

改表面 = 改架构决策,必须同步改本 allowlist 并在 commit message 说明。
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

from tgmonitor.core.app_service import AppService
from tgmonitor.core.events import EventBus

ALLOWED_METHODS = {
    # 生命周期
    "bootstrap",
    "subscribe_updates",
    "start_monitor",
    "stop_monitor",
    "pause_monitor",
    "resume_monitor",
    "shutdown",
    # 热重载
    "reconfigure",
    "validate_backends",
    # 导出
    "export",
    "export_media_list",
}

ALLOWED_PROPERTIES = {"monitor", "objects", "storage", "downloader", "is_paused"}

ALLOWED_INSTANCE_ATTRS = {
    # 后端 / 配置持有
    "bus",
    "client",
    "settings",
    "env_path",
    # 域服务
    "auth",
    "channels",
    "messages",
    "media",
    "batch",
    "channel_sync",
}


def _make_app() -> AppService:
    settings = MagicMock()
    settings.media_policy = "full"
    settings.paused = False
    objects = AsyncMock()
    objects.backend_name = "fake"
    return AppService(EventBus(), AsyncMock(), AsyncMock(), objects, settings)


def test_public_methods_allowlist() -> None:
    """类上公开方法(非 property / 非 dunder)必须恰好等于 allowlist。"""
    actual = {
        name
        for name, member in inspect.getmembers(AppService)
        if not name.startswith("_") and inspect.isfunction(member)
    }
    assert actual == ALLOWED_METHODS, (
        f"AppService 公开方法表面变更!\n"
        f"  新增(域转发?应加在域服务上): {sorted(actual - ALLOWED_METHODS)}\n"
        f"  移除: {sorted(ALLOWED_METHODS - actual)}"
    )


def test_public_properties_allowlist() -> None:
    """类上 property 必须恰好等于 allowlist。"""
    actual = {
        name
        for name, member in inspect.getmembers(AppService)
        if isinstance(member, property) and not name.startswith("_")
    }
    assert actual == ALLOWED_PROPERTIES, (
        f"AppService 公开 property 表面变更!\n"
        f"  新增: {sorted(actual - ALLOWED_PROPERTIES)}\n"
        f"  移除: {sorted(ALLOWED_PROPERTIES - actual)}"
    )


def test_public_instance_attrs_allowlist() -> None:
    """构造后实例公开属性(非下划线)必须恰好等于 allowlist。"""
    app = _make_app()
    actual = {name for name in vars(app) if not name.startswith("_")}
    assert actual == ALLOWED_INSTANCE_ATTRS, (
        f"AppService 公开实例属性表面变更!\n"
        f"  新增: {sorted(actual - ALLOWED_INSTANCE_ATTRS)}\n"
        f"  移除: {sorted(ALLOWED_INSTANCE_ATTRS - actual)}"
    )
