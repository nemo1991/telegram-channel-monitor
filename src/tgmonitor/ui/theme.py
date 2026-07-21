"""主题管理 — 浅色 / 暗色切换。

通过 QApplication.setStyleSheet(QApplication.instance(), qss) 应用。
缓存当前主题,提供 toggle() 在两主题间切换。

设计:
  - 不在 nav_bar.py 内联样式中冲突(暗色导航栏单独样式由 nav_bar
    自己处理:setStyleSheet 时使用 is_dark 参数)
  - SearchBar 内联样式在 init 时固定,不依赖主题切换(浅色足够通用)
"""
from __future__ import annotations

from enum import Enum
from importlib import resources


class Theme(Enum):
    LIGHT = "light"
    DARK = "dark"


class ThemeManager:
    """进程级单例。"""

    _current: Theme = Theme.LIGHT

    @classmethod
    def current(cls) -> Theme:
        return cls._current

    @classmethod
    def load_qss(cls, theme: Theme) -> str:
        if theme == Theme.DARK:
            return resources.files("tgmonitor.ui.resources").joinpath("style_dark.qss").read_text("utf-8")
        return resources.files("tgmonitor.ui.resources").joinpath("style.qss").read_text("utf-8")

    @classmethod
    def apply(cls, theme: Theme) -> None:
        """应用主题到 QApplication。"""
        from PySide6.QtWidgets import QApplication

        cls._current = theme
        app = QApplication.instance()
        if app is None:
            return
        qss = cls.load_qss(theme)
        app.setStyleSheet(qss)

    @classmethod
    def toggle(cls) -> Theme:
        """切换主题,返回新主题。"""
        new = Theme.DARK if cls._current == Theme.LIGHT else Theme.LIGHT
        cls.apply(new)
        return new