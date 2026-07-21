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
        """应用主题到 QApplication。同时把 accent 注入 dynamic property,
        QSS 内部可用 qApp.property("accent") 拿到(注意:Qt QSS 不直接
        支持 property(),要拿的话只能在 apply() 时做一次字符串替换)。"""
        from PySide6.QtWidgets import QApplication

        cls._current = theme
        app = QApplication.instance()
        if app is None:
            return
        qss = cls.load_qss(theme)
        # 把 {accent} / {accentHover} 占位符替换成实际 hex,让 QSS 走单源
        qss = qss.replace("{accent}", cls.accent()).replace(
            "{accentHover}", cls.accent("hover")
        )
        app.setStyleSheet(qss)

    @classmethod
    def toggle(cls) -> Theme:
        """切换主题,返回新主题。"""
        new = Theme.DARK if cls._current == Theme.LIGHT else Theme.LIGHT
        cls.apply(new)
        return new

    # ---- accent token 表(集中配色,避免散落 hex) ----
    # 用法:ThemeManager.accent() / ThemeManager.accent("hover")
    # nav_bar.py 仍用本地 _palette 自包含(主题切换时再读 ThemeManager.current);
    # QSS 走 app.setProperty("accent", ...) 注入(见 apply())。
    ACCENT_LIGHT = "#5b9cf5"
    ACCENT_LIGHT_HOVER = "#4a8be4"
    ACCENT_DARK = "#7bb4ff"
    ACCENT_DARK_HOVER = "#a3c8ff"

    @classmethod
    def accent(cls, kind: str = "default") -> str:
        """返回当前主题下的 accent 色。

        kind:
          - "default": 常规 accent(按钮底色 / 边线 / icon tint)
          - "hover":   hover 态(更亮一阶)
        """
        if cls._current == Theme.DARK:
            return cls.ACCENT_DARK_HOVER if kind == "hover" else cls.ACCENT_DARK
        return cls.ACCENT_LIGHT_HOVER if kind == "hover" else cls.ACCENT_LIGHT