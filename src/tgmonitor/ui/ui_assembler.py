"""UiAssembler — `MainWindow` 装配抽提。

2026-10-12 plan-batch3 目标 1:`main_window.py` 982 → ~864 LOC。

把 5 段装配逻辑(共 ~264 LOC)从 `MainWindow` 抽到本类:
- `_build_central_widget`(115 LOC)— widget hierarchy + 5 page + status bar + 2 debounce
- `_build_menu`(31 LOC)— File menu
- `_build_tray`(15 LOC)— TrayIcon
- `_wire_signals`(42 LOC)— 内部 signal 连接
- `_wire_events`(46 LOC)— VM event 连接

设计原则:
- **plain class**(不是 QObject)—— `UiAssembler` 不持 Qt 主线程所有权,
  只是 build → return widget dict,再走 signal wire。
- **持 `MainWindow` 引用** —— 仅 wire 阶段 callback 用(如 `dashboard.on_refresh = self._mw._on_refresh_channels` 这种 attribute-style bind),build 阶段不依赖 main_window 引用。
- **`MainWindowCtx` 持有 widget refs** —— 返回的 widget dict 由 MainWindow
  显式赋值给 ctx 字段,ctx 不持有 UiAssembler 引用(避免循环)。

为什么这样切:
- `MainWindow.__init__` 装配顺序敏感(ctx 先就位 → controllers 实例化 →
  signal wire 顺序),plain class UiAssembler 不打乱这个顺序。
- `_wire_*` 方法的 `self.X.connect(...)` 需要 access MainWindow 的 widget 引用,
  UiAssembler 持 `main_window` 引用后等价于原 `self.X.connect(...)`。

不做:
- 不抽 controller(已有 15 controller + MainWindowCtx,plan-batch2 已落点)
- 不抽 `_build_actions` / `_build_menus` / `_build_toolbars`(中央 widget 装配在
  `_build_central_widget` 内部,不分更细)
- 不改 `_setup_core` / `_setup_controllers`(这两个是 stage + ctx 装配逻辑,
  plan-batch2 阶段 3.2 已拆)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tgmonitor.ui.controllers._base import MainWindowCtx

if TYPE_CHECKING:
    from tgmonitor.ui.main_window import MainWindow


class UiAssembler:
    """Pure widget hierarchy + signal orchestration tool.

    plan-batch3 目标 1 step 1.1:empty class shell,MainWindowCtx hook ready。
    后续 1.2-1.5 step 把 5 段装配逻辑搬过来。
    """

    def __init__(self, ctx: MainWindowCtx, main_window: MainWindow) -> None:
        """保存 ctx + main_window 引用,不持有任何 Qt 资源。"""
        self._ctx = ctx
        self._mw = main_window

    def assemble(self) -> dict[str, object]:
        """编排入口 —— plan-batch3 目标 1 step 1.5 落地。"""
        msg = "assemble() is not yet wired — see plan-batch3-1.2~1.5"
        raise NotImplementedError(msg)
