"""Theme 切换 controller — `_on_theme_toggle` / `_on_theme_changed`。

2026-10-01 v1.12.x 从 `MainWindow` 抽出。原 handler 各 ~20 LOC,加起来 ~40,
含 ThemeManager 局部 import;挪到这里集中,MainWindow 留 1 行 delegate。

信号契约:
- `header.btn_theme.clicked` → `_on_theme_toggle`(用户主动切)
- `ThemeManager._instance().theme_changed` → `_on_theme_changed`(OS 切色兜底)
"""

from __future__ import annotations

from tgmonitor.ui.controllers._base import MainWindowCtx


class ThemeController:
    """主题切换 + OS 切色兜底。"""

    def __init__(self, ctx: MainWindowCtx) -> None:
        self._ctx = ctx

    def toggle(self) -> None:
        """用户点主题按钮。"""
        from tgmonitor.ui.theme import ThemeManager

        new = ThemeManager.toggle()
        # 更新主题按钮图标
        self._ctx.header.btn_theme.setText("☀" if new.value == "dark" else "🌙")
        # 刷新 nav bar 内部样式
        self._ctx.nav.refresh_theme()
        # 频道类型图标(已 tinted)需要按新主题重画
        if hasattr(self._ctx.channel_panel, "refresh_theme"):
            self._ctx.channel_panel.refresh_theme()
        # 2026-09-07 v1.6.8:status bar 文案走 tr()。
        self._ctx.status_bar.show_message(
            self._tr("已切换到 {kind} 主题").format(
                kind=self._tr("暗色") if new.value == "dark" else self._tr("浅色")
            ),
            2000,
        )

    def on_theme_changed(self) -> None:
        """ThemeManager 主题变 → UI 同步(OS 切色兜底)。"""
        from tgmonitor.ui.theme import ThemeManager

        actual = ThemeManager.actual()
        # 按钮图标按 actual(非 current)—— SYSTEM 态下按 OS 实际值显示
        self._ctx.header.btn_theme.setText("☀" if actual.value == "dark" else "🌙")
        self._ctx.nav.refresh_theme()
        if hasattr(self._ctx.channel_panel, "refresh_theme"):
            self._ctx.channel_panel.refresh_theme()
        # 2026-09-07 v1.6.8:status bar 文案走 tr()。
        self._ctx.status_bar.show_message(
            self._tr("已切换到 {kind} 主题").format(
                kind=self._tr("暗色") if actual.value == "dark" else self._tr("浅色")
            ),
            2000,
        )

    def _tr(self, src: str) -> str:
        """翻译入口 — 保留 `MainWindow` context,沿用现有 zh_CN.ts / en_US.ts 翻译。

        原 MainWindow 自有 `self.tr(src)`,context = `MainWindow`;controller
        没 QWidget 引用,走 `QCoreApplication.translate` 全局入口。
        """
        from PySide6.QtCore import QCoreApplication

        return QCoreApplication.translate("MainWindow", src)
