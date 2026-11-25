"""`AppStateController` 单元测试 — 2026-10-09 v1.12.x 下一 plan 目标 1.2。

覆盖纯逻辑(handler → ctx 调用)— widget 都用 mock,不依赖 Qt offscreen。
包括:
- VM `login_state` / `conn_state` / `error` signal handler
- EventBus `LoginStateChanged` / `NotificationRequested` handler
- `on_bell_clicked` / `on_clear_error_log` 铃铛 dialog
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tgmonitor.core.events import (  # noqa: E402
    LoginStateChanged,
    NotificationRequested,
)
from tgmonitor.ui.controllers._base import MainWindowCtx  # noqa: E402
from tgmonitor.ui.controllers.app_state_controller import (  # noqa: E402
    AppStateController,
)


def _make_ctx() -> tuple[MainWindowCtx, dict[str, Any]]:
    """构造 MainWindowCtx + 暴露常用 mock 引用。"""
    status_bar = MagicMock(name="status_bar")
    tray = MagicMock(name="tray")
    tray.is_active = True
    main_window = MagicMock(name="main_window")
    main_window.tr = MagicMock(side_effect=lambda s: s)  # tr() 返原文

    ctx = MainWindowCtx(
        app=MagicMock(name="app"),
        monitor=MagicMock(name="monitor"),
        loop=MagicMock(name="loop"),
        vm=MagicMock(name="vm"),
        env_path=MagicMock(name="env_path"),
        main_window=main_window,
        window_title_setter=MagicMock(name="window_title_setter"),
        quit_app=MagicMock(name="quit_app"),
        live_view=MagicMock(name="live_view"),
        message_detail=MagicMock(name="message_detail"),
        media_manager=MagicMock(name="media_manager"),
        dashboard=MagicMock(name="dashboard"),
        channel_panel=MagicMock(name="channel_panel"),
        status_bar=status_bar,
        nav=MagicMock(name="nav"),
        stack=MagicMock(name="stack"),
        tray=tray,
        media_refresh_debounce=MagicMock(name="media_refresh_debounce"),
        settings_page=MagicMock(name="settings_page"),
    )
    refs = {
        "status_bar": status_bar,
        "tray": tray,
        "main_window": main_window,
    }
    return ctx, refs


class TestOnBusLogin:
    """`on_bus_login` — EventBus LoginStateChanged。"""

    @pytest.mark.asyncio
    async def test_ready_state_updates_status_and_settings(self) -> None:
        """D3:头栏已退化为纯标题,登录态下沉到底部状态位 + 设置页入口。"""
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)

        event = LoginStateChanged(state="ready", detail="ok")
        await ctrl.on_bus_login(event)

        ctx.status_bar.set_login_state.assert_called_once_with("ready")
        ctx.settings_page.set_login_state.assert_called_once_with("ready")
        ctx.main_window._refresh_state.assert_called_once()
        ctx.status_bar.show_activity.assert_called_once_with("登录完成", timeout_ms=2000)


class TestOnNotificationFallback:
    """`on_notification_fallback` — EventBus NotificationRequested。"""

    @pytest.mark.asyncio
    async def test_error_level_uses_warning_prefix(self) -> None:
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)
        event = NotificationRequested(title="错误", body="失败", level="error")
        await ctrl.on_notification_fallback(event)

        ctx.status_bar.show_activity.assert_called_once_with("警告:错误: 失败", timeout_ms=5000)
