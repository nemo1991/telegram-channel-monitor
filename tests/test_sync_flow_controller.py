"""`SyncFlowController` 单元测试 — 2026-10-09 v1.12.x 下一 plan 目标 1.1。

覆盖纯逻辑:
- `_build_sync_titles` — VM.known_channels 有/无 title 两种 case
- `_show_sync_options_dialog` — 取消返 None,确认返 options
- `_wire_sync_progress_signals` — VM signal 连到 dialog slot
- `on_sync_requested` 集成 — options=None 提前返回,options 走 progress dialog
  + run_coro
- 旧 main_window 1,148 LOC 透传 thin delegate 行为保留

为什么用 mock:controller 4 个 helper 全走 ctx,不需要 qapp。
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


from tgmonitor.core.dto import SyncOptions  # noqa: E402
from tgmonitor.ui.controllers._base import MainWindowCtx  # noqa: E402
from tgmonitor.ui.controllers.sync_flow_controller import (  # noqa: E402
    SyncFlowController,
)


def _make_ctx() -> tuple[MainWindowCtx, dict[str, Any]]:
    """构造 MainWindowCtx + 暴露常用 mock 引用。"""
    vm = MagicMock(name="vm")
    vm.known_channels = {}  # 默认空 — 测试可改
    vm.sync_progress = MagicMock(name="sync_progress")
    vm.sync_done = MagicMock(name="sync_done")

    app = MagicMock(name="app")
    app.settings.sync_chat_delay_ms = 100
    app.settings.sync_page_delay_ms = 200
    app.settings.sync_resume_from_saved = True
    app.channel_sync.cancel = MagicMock(name="channel_sync.cancel")
    app.sync_channels = MagicMock(name="sync_channels")

    main_window = MagicMock(name="main_window")

    ctx = MainWindowCtx(
        app=app,
        monitor=MagicMock(name="monitor"),
        loop=MagicMock(name="loop"),
        vm=vm,
        env_path=MagicMock(name="env_path"),
        main_window=main_window,
        window_title_setter=MagicMock(name="window_title_setter"),
        quit_app=MagicMock(name="quit_app"),
        live_view=MagicMock(name="live_view"),
        message_detail=MagicMock(name="message_detail"),
        media_manager=MagicMock(name="media_manager"),
        selection_toolbar=MagicMock(name="selection_toolbar"),
        dashboard=MagicMock(name="dashboard"),
        channel_panel=MagicMock(name="channel_panel"),
        header=MagicMock(name="header"),
        status_bar=MagicMock(name="status_bar"),
        nav=MagicMock(name="nav"),
        stack=MagicMock(name="stack"),
        tray=None,
        search_debounce=MagicMock(name="search_debounce"),
        media_refresh_debounce=MagicMock(name="media_refresh_debounce"),
    )
    refs = {
        "vm": vm,
        "app": app,
        "main_window": main_window,
    }
    return ctx, refs


class TestBuildSyncTitles:
    """`_build_sync_titles` — VM.known_channels 有/无回退 `#<id>`。"""

    def test_known_channels_returns_title(self) -> None:
        """VM 有 DTO → 用 `.title`。"""
        ctx, refs = _make_ctx()
        ch = MagicMock()
        ch.title = "Channel A"
        refs["vm"].known_channels = {1: ch}

        ctrl = SyncFlowController(ctx)
        result = ctrl._build_sync_titles([1])

        assert result == {1: "Channel A"}

    def test_unknown_channel_falls_back_to_hash_id(self) -> None:
        """VM 没找到(VMe 还没 bootstrap)→ 回退 `#<id>`。"""
        ctx, refs = _make_ctx()
        refs["vm"].known_channels = {}

        ctrl = SyncFlowController(ctx)
        result = ctrl._build_sync_titles([42, 99])

        assert result == {42: "#42", 99: "#99"}

    def test_mixed_known_and_unknown(self) -> None:
        """部分已知 + 部分未知 — 各自走正确分支。"""
        ctx, refs = _make_ctx()
        ch = MagicMock()
        ch.title = "Channel A"
        refs["vm"].known_channels = {1: ch}

        ctrl = SyncFlowController(ctx)
        result = ctrl._build_sync_titles([1, 2])

        assert result == {1: "Channel A", 2: "#2"}


class TestShowSyncOptionsDialog:
    """`_show_sync_options_dialog` — 取消/确认分支。"""

    def test_user_cancel_returns_none(self) -> None:
        """`dlg.exec() == False` → 返 None(用户取消)。"""
        ctx, _refs = _make_ctx()
        ctrl = SyncFlowController(ctx)

        with patch("tgmonitor.ui.controllers.sync_flow_controller.SyncOptionsDialog") as mock_dlg:
            dlg = mock_dlg.return_value
            dlg.exec.return_value = False
            result = ctrl._show_sync_options_dialog([1], {1: "title"})

        assert result is None
        mock_dlg.assert_called_once()
        # defaults 必须从 settings 拉 — 验证 chk
        call_args = mock_dlg.call_args
        defaults = call_args.kwargs.get("defaults") or call_args.args[2]
        assert isinstance(defaults, SyncOptions)
        assert defaults.chat_delay_ms == 100
        assert defaults.page_delay_ms == 200
        assert defaults.resume_from_saved is True

    def test_user_confirm_returns_options(self) -> None:
        """`dlg.exec() == True` → 返 `dlg.options()` 结果。"""
        ctx, _refs = _make_ctx()
        ctrl = SyncFlowController(ctx)
        expected_options = SyncOptions(
            chat_delay_ms=300,
            page_delay_ms=400,
            resume_from_saved=False,
        )

        with patch("tgmonitor.ui.controllers.sync_flow_controller.SyncOptionsDialog") as mock_dlg:
            dlg = mock_dlg.return_value
            dlg.exec.return_value = True
            dlg.options.return_value = expected_options
            result = ctrl._show_sync_options_dialog([1], {1: "title"})

        assert result is expected_options
        dlg.options.assert_called_once()


class TestWireSyncProgressSignals:
    """`_wire_sync_progress_signals` — VM signal 连到 dialog slot。"""

    def test_connects_both_signals(self) -> None:
        """VM.sync_progress / VM.sync_done → dialog.on_progress / on_done。"""
        ctx, _refs = _make_ctx()
        ctrl = SyncFlowController(ctx)
        dialog = MagicMock()

        ctrl._wire_sync_progress_signals(dialog)

        ctx.vm.sync_progress.connect.assert_called_once_with(dialog.on_progress)
        ctx.vm.sync_done.connect.assert_called_once_with(dialog.on_done)


class TestOnSyncRequested:
    """`on_sync_requested` 集成 — 4 个 helper + run_coro 编排。"""

    def test_options_none_returns_early(self) -> None:
        """`_show_sync_options_dialog` 返 None → 不开 progress dialog,不 fire coroutine。"""
        ctx, _refs = _make_ctx()
        ctrl = SyncFlowController(ctx)

        with (
            patch.object(ctrl, "_show_sync_options_dialog", return_value=None) as mock_show,
            patch(
                "tgmonitor.ui.controllers.sync_flow_controller.SyncProgressDialog"
            ) as mock_prog_dlg,
        ):
            ctrl.on_sync_requested([1, 2])

        mock_show.assert_called_once()
        # dialog 不应该被构造
        mock_prog_dlg.assert_not_called()
        # run_coro 不应该被调
        ctx.app.sync_channels.assert_not_called()

    def test_full_flow_with_options(self) -> None:
        """options 有值 → 构造 progress dialog + connect signals + run_coro + exec。"""
        ctx, _refs = _make_ctx()
        ctrl = SyncFlowController(ctx)
        options = SyncOptions(chat_delay_ms=100, page_delay_ms=200, resume_from_saved=True)

        with (
            patch.object(ctrl, "_show_sync_options_dialog", return_value=options),
            patch(
                "tgmonitor.ui.controllers.sync_flow_controller.SyncProgressDialog"
            ) as mock_prog_dlg,
        ):
            dlg = mock_prog_dlg.return_value
            dlg.exec.return_value = True
            with patch("tgmonitor.ui.controllers.sync_flow_controller.run_coro") as mock_run_coro:
                ctrl.on_sync_requested([1])

        mock_prog_dlg.assert_called_once()
        call_args = mock_prog_dlg.call_args
        # 第一个位置参数 = titles dict
        assert call_args.args[0] == {1: "#1"}
        # cancel_cb 是 app.channel_sync.cancel
        assert call_args.kwargs.get("cancel_cb") is ctx.app.channel_sync.cancel
        # parent 是 main_window
        assert call_args.kwargs.get("parent") is ctx.main_window
        # signals wire
        ctx.vm.sync_progress.connect.assert_called_once_with(dlg.on_progress)
        ctx.vm.sync_done.connect.assert_called_once_with(dlg.on_done)
        # run_coro fire
        mock_run_coro.assert_called_once()
        # dialog.exec
        dlg.exec.assert_called_once()

    def test_signal_disconnect_in_finally_block(self) -> None:
        """`_go()` finally 必须 disconnect VM signals 防 dangling slot。

        不真跑 coroutine — 用 MagicMock 代替 `run_coro`,验证传给 run_coro
        的 coroutine 被 await 后 finally 块执行 disconnect。
        """
        import asyncio

        ctx, _refs = _make_ctx()

        # app.sync_channels 必须是 awaitable(MagicMock 默认不是)
        async def _fake_sync(*_a, **_kw):
            return None

        ctx.app.sync_channels = MagicMock(side_effect=_fake_sync)

        ctrl = SyncFlowController(ctx)
        options = SyncOptions(chat_delay_ms=100, page_delay_ms=200, resume_from_saved=True)

        with (
            patch.object(ctrl, "_show_sync_options_dialog", return_value=options),
            patch(
                "tgmonitor.ui.controllers.sync_flow_controller.SyncProgressDialog"
            ) as mock_prog_dlg,
        ):
            dlg = mock_prog_dlg.return_value
            with patch("tgmonitor.ui.controllers.sync_flow_controller.run_coro") as mock_run_coro:
                ctrl.on_sync_requested([1])

        # 拿出传给 run_coro 的 coroutine,同步跑完
        coro = mock_run_coro.call_args.args[1]
        # 用 asyncio.run 跑完(finally 必跑)
        ctx.vm.sync_progress.disconnect = MagicMock()
        ctx.vm.sync_done.disconnect = MagicMock()
        asyncio.run(coro)

        ctx.vm.sync_progress.disconnect.assert_called_with(dlg.on_progress)
        ctx.vm.sync_done.disconnect.assert_called_with(dlg.on_done)

    def test_signal_disconnect_swallows_runtimer_error(self) -> None:
        """disconnect 抛 RuntimeError(已 GC)→ swallow,不重抛。"""
        import asyncio

        ctx, _refs = _make_ctx()

        async def _fake_sync(*_a, **_kw):
            return None

        ctx.app.sync_channels = MagicMock(side_effect=_fake_sync)

        ctrl = SyncFlowController(ctx)
        options = SyncOptions(chat_delay_ms=100, page_delay_ms=200, resume_from_saved=True)

        with (
            patch.object(ctrl, "_show_sync_options_dialog", return_value=options),
            patch("tgmonitor.ui.controllers.sync_flow_controller.SyncProgressDialog"),
            patch("tgmonitor.ui.controllers.sync_flow_controller.run_coro") as mock_run_coro,
        ):
            ctrl.on_sync_requested([1])

        coro = mock_run_coro.call_args.args[1]
        ctx.vm.sync_progress.disconnect = MagicMock(
            side_effect=RuntimeError("already disconnected")
        )
        ctx.vm.sync_done.disconnect = MagicMock()
        # 不应抛
        asyncio.run(coro)
