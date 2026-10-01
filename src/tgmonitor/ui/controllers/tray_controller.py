# mypy: disable-error-code=attr-defined
# 访问 `MainWindow._quit_app` — controller / MainWindow thin delegate
# 共享同一方法。
"""Tray / 监听暂停-恢复 controller — VM 转发过来的 quit / pause / resume。

2026-10-01 v1.12.x 从 `MainWindow` 抽出。3 个 handler:
- `_on_vm_quit_requested`:`QuitRequested` signal(由 tray menu「退出」发出)
- `_on_monitoring_paused`:`MonitoringPaused` signal(status bar + window title)
- `_on_monitoring_resumed`:`MonitoringResumed` signal(status bar + window title)

`_quit_app` 是 MainWindow 内部 utility — controller 直接调
`ctx.main_window._quit_app()`,走 MainWindow 引用,无需 callback 注入。
"""

from __future__ import annotations

from tgmonitor.ui.controllers._base import MainWindowCtx


class TrayController:
    """tray / 监听暂停-恢复 — VM signal 转 MainWindow 内部状态。"""

    def __init__(self, ctx: MainWindowCtx) -> None:
        self._ctx = ctx

    def on_vm_quit_requested(self) -> None:
        """tray menu「退出」→ VM 转发 → 真退出。

        与 MainWindow._quit_app 同义,但走 tray「退出」(不绕开 Qt 主循环)路径:
        VM `quit_requested` signal → 此 handler → `qt_app.quit`。
        """
        self._ctx.main_window._quit_app()

    def on_monitoring_paused(self, source: str) -> None:
        """2026-09-03 v1.6.1:监听已暂停 — 状态栏常驻 label 显示 +
        window title 加 `(⏸ 暂停)` 后缀。

        2026-10-01 v1.11.x 状态栏组件化:status_bar 子组件自管 paused label
        显隐,MainWindow 只委托 + 改 title。
        """
        self._ctx.status_bar.set_paused(True)
        from PySide6.QtCore import QCoreApplication

        base_title = QCoreApplication.translate("MainWindow", "tgmonitor · Telegram 频道监听")
        self._ctx.main_window.setWindowTitle(
            f"{base_title}  ({QCoreApplication.translate('MainWindow', '⏸ 暂停')})"
        )

    def on_monitoring_resumed(self, source: str) -> None:
        """2026-09-03 v1.6.1:监听已恢复 — 状态栏 label 隐藏 + title 复位。"""
        self._ctx.status_bar.set_paused(False)
        from PySide6.QtCore import QCoreApplication

        self._ctx.main_window.setWindowTitle(
            QCoreApplication.translate("MainWindow", "tgmonitor · Telegram 频道监听")
        )
