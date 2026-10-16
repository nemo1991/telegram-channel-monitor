"""`TrayController` 单元测试 — 2026-10-04 plan 阶段 3.4。

3 handler × 反向调用通道 callback(`window_title_setter` / `quit_app`):
- `on_vm_quit_requested` → `ctx.quit_app()`(不再回退)
- `on_monitoring_paused` → `ctx.window_title_setter(...)`(不再调 main_window.setWindowTitle)
- `on_monitoring_resumed` → `ctx.window_title_setter(...)`

为什么用 mock:
- controller 不持有 MainWindow 引用反向调 — 测 controller 等价于测
  「正确的 ctx 回调字段被正确调用」
- 不需要 qapp:mock 对象无 Qt 元对象系统需求
- 与 `test_main_window_tray.py`(主路径 `_quit_app` / closeEvent)互补:
  本测试是 controller 编排逻辑回归守门
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tgmonitor.ui.controllers._base import MainWindowCtx  # noqa: E402
from tgmonitor.ui.controllers.tray_controller import TrayController  # noqa: E402


def _make_ctx() -> tuple[MainWindowCtx, dict[str, MagicMock]]:
    """构造 MainWindowCtx + 暴露常用 mock 引用给测试体。

    window_title_setter / quit_app 是 2026-10-04 plan 阶段 3.4 注入的
    反向调用 callback,controller 不直接调 `main_window.setWindowTitle` /
    `main_window._quit_app`。
    """
    status_bar = MagicMock(name="status_bar")
    window_title_setter = MagicMock(name="window_title_setter")
    quit_app = MagicMock(name="quit_app")
    ctx = MainWindowCtx(
        app=MagicMock(name="app"),
        monitor=MagicMock(name="monitor"),
        loop=MagicMock(name="loop"),
        vm=MagicMock(name="vm"),
        env_path=MagicMock(name="env_path"),
        main_window=MagicMock(name="main_window"),
        window_title_setter=window_title_setter,
        quit_app=quit_app,
        live_view=MagicMock(name="live_view"),
        # 2026-10-15 plan-batch7-C-α:LiveActionsController 注入 ctx
        live_actions=MagicMock(name="live_actions"),
        message_detail=MagicMock(name="message_detail"),
        media_manager=MagicMock(name="media_manager"),
        selection_toolbar=MagicMock(name="selection_toolbar"),
        dashboard=MagicMock(name="dashboard"),
        channel_panel=MagicMock(name="channel_panel"),
        header=MagicMock(name="header"),
        status_bar=status_bar,
        nav=MagicMock(name="nav"),
        stack=MagicMock(name="stack"),
        tray=None,
        search_debounce=MagicMock(name="search_debounce"),
        media_refresh_debounce=MagicMock(name="media_refresh_debounce"),
    )
    refs = {
        "status_bar": status_bar,
        "window_title_setter": window_title_setter,
        "quit_app": quit_app,
    }
    return ctx, refs


# ============================================================
# `on_vm_quit_requested` 行为
# ============================================================


def test_on_vm_quit_requested_calls_quit_app_callback() -> None:
    """tray menu「退出」→ VM 转发 → `ctx.quit_app()` 被调到。"""
    ctx, refs = _make_ctx()
    ctrl = TrayController(ctx)
    ctrl.on_vm_quit_requested()
    refs["quit_app"].assert_called_once_with()


def test_on_vm_quit_requested_does_not_touch_status_bar() -> None:
    """quit 流程只走 ctx 回调,不直接刷 status_bar。"""
    ctx, refs = _make_ctx()
    ctrl = TrayController(ctx)
    ctrl.on_vm_quit_requested()
    refs["status_bar"].set_paused.assert_not_called()


# ============================================================
# `on_monitoring_paused` 行为
# ============================================================


def test_on_monitoring_paused_calls_set_paused_and_title_setter() -> None:
    """监听暂停 → status_bar.set_paused(True) + window_title_setter(...)。

    2026-10-04 plan 阶段 3.4:title 改走 callback,不再直接调
    `main_window.setWindowTitle(...)`。
    """
    ctx, refs = _make_ctx()
    ctrl = TrayController(ctx)
    ctrl.on_monitoring_paused(source="tray_menu")
    refs["status_bar"].set_paused.assert_called_once_with(True)
    refs["window_title_setter"].assert_called_once()
    # 文案应含「暂停」后缀,base 是项目产品名
    args = refs["window_title_setter"].call_args.args
    assert "暂停" in args[0]
    assert "tgmonitor" in args[0]


# ============================================================
# `on_monitoring_resumed` 行为
# ============================================================


def test_on_monitoring_resumed_calls_set_paused_false_and_resets_title() -> None:
    """监听恢复 → status_bar.set_paused(False) + window_title_setter 基线 title。

    基线 title 不含「暂停」后缀,只含产品名。
    """
    ctx, refs = _make_ctx()
    ctrl = TrayController(ctx)
    ctrl.on_monitoring_resumed(source="tray_menu")
    refs["status_bar"].set_paused.assert_called_once_with(False)
    refs["window_title_setter"].assert_called_once()
    args = refs["window_title_setter"].call_args.args
    assert "暂停" not in args[0]
    assert "tgmonitor" in args[0]


# ============================================================
# 反向调用通道守门:不直接访问 main_window 反向方法
# ============================================================


def test_tray_controller_does_not_call_main_window_set_window_title() -> None:
    """2026-10-04 plan 阶段 3.4 守门:handler 跑完后 main_window.setWindowTitle
    mock 不应被调到(title 走 ctx.window_title_setter)。

    这是「反向调用通道」的负面测试 — 防回归到「直接调 main_window 私有 /
    public 方法」。
    """
    ctx, refs = _make_ctx()
    # 把 main_window 本身替换成 MagicMock,任何反向调用会落到它身上
    ctx_main_window_mock = MagicMock(name="main_window_real_ref")
    # dataclass 不可直接 setattr,改为构造新 ctx 模拟 main_window 调用
    ctx_real = MainWindowCtx(
        app=ctx.app,
        monitor=ctx.monitor,
        loop=ctx.loop,
        vm=ctx.vm,
        env_path=ctx.env_path,
        main_window=ctx_main_window_mock,
        window_title_setter=refs["window_title_setter"],
        quit_app=refs["quit_app"],
        live_view=ctx.live_view,
        # 2026-10-15 plan-batch7-C-α:LiveActionsController 注入 ctx
        live_actions=MagicMock(name="live_actions"),
        message_detail=ctx.message_detail,
        media_manager=ctx.media_manager,
        selection_toolbar=ctx.selection_toolbar,
        dashboard=ctx.dashboard,
        channel_panel=ctx.channel_panel,
        header=ctx.header,
        status_bar=ctx.status_bar,
        nav=ctx.nav,
        stack=ctx.stack,
        tray=ctx.tray,
        search_debounce=ctx.search_debounce,
        media_refresh_debounce=ctx.media_refresh_debounce,
    )
    ctrl = TrayController(ctx_real)
    ctrl.on_vm_quit_requested()
    ctrl.on_monitoring_paused(source="tray_menu")
    ctrl.on_monitoring_resumed(source="tray_menu")
    # main_window 自己不应收到 setWindowTitle / _quit_app 调用
    ctx_main_window_mock.setWindowTitle.assert_not_called()
    ctx_main_window_mock._quit_app.assert_not_called()
