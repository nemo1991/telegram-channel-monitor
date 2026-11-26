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

from tgmonitor.ui.controllers._base import MainWindowCtx
from tgmonitor.ui.controllers.tray_controller import TrayController


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
        message_detail=MagicMock(name="message_detail"),
        media_manager=MagicMock(name="media_manager"),
        dashboard=MagicMock(name="dashboard"),
        channel_panel=MagicMock(name="channel_panel"),
        status_bar=status_bar,
        nav=MagicMock(name="nav"),
        stack=MagicMock(name="stack"),
        tray=None,
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


# ============================================================
# 反向调用通道守门:不直接访问 main_window 反向方法
# ============================================================
