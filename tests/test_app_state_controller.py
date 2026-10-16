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
from unittest.mock import MagicMock, patch

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
    header = MagicMock(name="header")
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
        # 2026-10-15 plan-batch7-C-α:LiveActionsController 注入 ctx
        live_actions=MagicMock(name="live_actions"),
        message_detail=MagicMock(name="message_detail"),
        media_manager=MagicMock(name="media_manager"),
        selection_toolbar=MagicMock(name="selection_toolbar"),
        dashboard=MagicMock(name="dashboard"),
        channel_panel=MagicMock(name="channel_panel"),
        header=header,
        status_bar=status_bar,
        nav=MagicMock(name="nav"),
        stack=MagicMock(name="stack"),
        tray=tray,
        search_debounce=MagicMock(name="search_debounce"),
        media_refresh_debounce=MagicMock(name="media_refresh_debounce"),
    )
    refs = {
        "status_bar": status_bar,
        "header": header,
        "tray": tray,
        "main_window": main_window,
    }
    return ctx, refs


class TestOnLoginState:
    """`on_login_state` — VM login_state signal。"""

    def test_ready_state_shows_completion_activity(self) -> None:
        ctx, refs = _make_ctx()
        ctrl = AppStateController(ctx)

        ctrl.on_login_state("ready")

        refs["status_bar"].show_message.assert_called_once()
        refs["status_bar"].show_activity.assert_called_once_with("登录完成", timeout_ms=2000)

    def test_logged_in_state_shows_completion_activity(self) -> None:
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)

        ctrl.on_login_state("logged_in")

        ctx.status_bar.show_activity.assert_called_once_with("登录完成", timeout_ms=2000)

    def test_error_state_shows_failure(self) -> None:
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)

        ctrl.on_login_state("error")

        ctx.status_bar.show_activity.assert_called_once_with("登录失败: error", timeout_ms=5000)

    def test_closed_state_shows_failure(self) -> None:
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)

        ctrl.on_login_state("closed")

        ctx.status_bar.show_activity.assert_called_once_with("登录失败: closed", timeout_ms=5000)

    def test_phone_required_state_shows_waiting(self) -> None:
        """phone_required / code_required / password_required → 等待用户输入。"""
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)

        ctrl.on_login_state("phone_required")

        ctx.status_bar.show_activity.assert_called_once_with("登录中: phone_required")


class TestOnConnState:
    """`on_conn_state` — VM conn_state signal。"""

    def test_ready_state_connects(self) -> None:
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)

        ctrl.on_conn_state("ready")

        ctx.status_bar.set_connection_state.assert_called_once_with("ready")
        ctx.status_bar.show_activity.assert_called_once_with("TG 已连接", timeout_ms=1500)

    def test_connecting_state_shows_connecting(self) -> None:
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)

        ctrl.on_conn_state("connecting")

        ctx.status_bar.show_activity.assert_called_once_with("TG 连接中: connecting")

    def test_waiting_for_network_state(self) -> None:
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)

        ctrl.on_conn_state("waiting_for_network")

        ctx.status_bar.show_activity.assert_called_once_with("等待网络…")


class TestOnError:
    """`on_error` — VM error signal。"""

    def test_error_logged_and_shown(self) -> None:
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)

        ctrl.on_error("网络断了")

        ctx.status_bar.show_message.assert_called_once_with("⚠ 网络断了", 5000)
        ctx.status_bar.show_activity.assert_called_once_with("⚠ 网络断了", timeout_ms=5000)


class TestOnBusLogin:
    """`on_bus_login` — EventBus LoginStateChanged。"""

    @pytest.mark.asyncio
    async def test_non_login_state_event_ignored(self) -> None:
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)

        # 传一个不是 LoginStateChanged 的对象 → 早 return
        await ctrl.on_bus_login("not an event")

        ctx.header.update_state.assert_not_called()
        ctx.main_window._refresh_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_ready_state_updates_header(self) -> None:
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)

        event = LoginStateChanged(state="ready", detail="ok")
        await ctrl.on_bus_login(event)

        ctx.header.update_state.assert_called_once_with("ready", "ok")
        ctx.main_window._refresh_state.assert_called_once()
        ctx.status_bar.show_activity.assert_called_once_with("登录完成", timeout_ms=2000)

    @pytest.mark.asyncio
    async def test_error_state_shows_failure(self) -> None:
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)

        event = LoginStateChanged(state="error", detail="bad token")
        await ctrl.on_bus_login(event)

        ctx.status_bar.show_activity.assert_called_once_with("登录失败: error", timeout_ms=5000)


class TestOnNotificationFallback:
    """`on_notification_fallback` — EventBus NotificationRequested。"""

    @pytest.mark.asyncio
    async def test_non_notification_event_ignored(self) -> None:
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)

        await ctrl.on_notification_fallback("not a notification")

        ctx.status_bar.show_message.assert_not_called()
        ctx.status_bar.show_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_tray_shows_message(self) -> None:
        from dataclasses import replace

        ctx, _refs = _make_ctx()
        ctx = replace(ctx, tray=None)  # 无 tray

        ctrl = AppStateController(ctx)
        event = NotificationRequested(title="新消息", body="hello", level="info")
        await ctrl.on_notification_fallback(event)

        ctx.status_bar.show_message.assert_called_once_with("新消息: hello", 5000)
        ctx.status_bar.show_activity.assert_called_once_with("新消息: hello", timeout_ms=5000)

    @pytest.mark.asyncio
    async def test_inactive_tray_shows_message(self) -> None:
        ctx, refs = _make_ctx()
        refs["tray"].is_active = False  # 有 tray 但 inactive

        ctrl = AppStateController(ctx)
        event = NotificationRequested(title="新消息", body="hello", level="info")
        await ctrl.on_notification_fallback(event)

        ctx.status_bar.show_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_active_tray_skips_message_but_shows_activity(self) -> None:
        """tray active 时不弹 show_message(系统通知已发),但仍给 activity 提示。"""
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)
        event = NotificationRequested(title="新消息", body="hello", level="info")
        await ctrl.on_notification_fallback(event)

        ctx.status_bar.show_message.assert_not_called()
        ctx.status_bar.show_activity.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_level_uses_warning_prefix(self) -> None:
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)
        event = NotificationRequested(title="错误", body="失败", level="error")
        await ctrl.on_notification_fallback(event)

        ctx.status_bar.show_activity.assert_called_once_with("⚠ 错误: 失败", timeout_ms=5000)

    @pytest.mark.asyncio
    async def test_warning_level_uses_lightning_prefix(self) -> None:
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)
        event = NotificationRequested(title="警告", body="注意", level="warning")
        await ctrl.on_notification_fallback(event)

        ctx.status_bar.show_activity.assert_called_once_with("⚡ 警告: 注意", timeout_ms=5000)


class TestOnBellClicked:
    """`on_bell_clicked` — 错误日志 dialog。"""

    def test_constructs_dialog_and_execs(self) -> None:
        ctx, refs = _make_ctx()
        refs["status_bar"].get_error_log.return_value = ["error 1", "error 2"]

        ctrl = AppStateController(ctx)

        with patch("tgmonitor.ui.widgets.error_log_dialog.ErrorLogDialog") as mock_dlg:
            dlg = mock_dlg.return_value
            ctrl.on_bell_clicked()

        mock_dlg.assert_called_once()
        # entries = get_error_log()
        assert mock_dlg.call_args.args[0] == ["error 1", "error 2"]
        # parent = main_window
        assert mock_dlg.call_args.kwargs.get("parent") is ctx.main_window
        # on_clear = on_clear_error_log method(bound method 比较)
        on_clear_cb = mock_dlg.call_args.kwargs.get("on_clear")
        assert on_clear_cb == ctrl.on_clear_error_log
        dlg.exec.assert_called_once()


class TestOnClearErrorLog:
    """`on_clear_error_log` — 清空错误日志。"""

    def test_delegates_to_status_bar(self) -> None:
        ctx, _refs = _make_ctx()
        ctrl = AppStateController(ctx)

        ctrl.on_clear_error_log()

        ctx.status_bar.clear_error_log.assert_called_once()
