# mypy: disable-error-code="attr-defined"
"""主题管理 — 浅色 / 暗色 / 跟随系统切换。

通过 QApplication.setStyleSheet(QApplication.instance(), qss) 应用。
缓存当前主题,提供 toggle() 在三主题间切换。

设计:
  - 不在 nav_bar.py 内联样式中冲突(暗色导航栏单独样式由 nav_bar
    自己处理:setStyleSheet 时使用 is_dark 参数)
  - SearchBar 内联样式在 init 时固定,不依赖主题切换(浅色足够通用)

# 2026-08-30 v1.5.0 PR #A5:新增 Theme.SYSTEM 态,ThemeManager.apply 监听
# QStyleHints.colorSchemeChanged(Qt 6.5+)— 系统切深色时 UI 自动跟随。
"""

from __future__ import annotations

import logging
from enum import Enum
from importlib import resources

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)


class Theme(Enum):
    """主题枚举:`light` / `dark` / `system`(跟随 OS)。value 小写字符串。"""

    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"  # 2026-08-30 v1.5.0 PR #A5:跟随 OS 主题


class ThemeManager(QObject):
    """进程级单例 — Qt 基类以提供 `theme_changed` signal 让 nav_bar / 其它
    widget 主动监听 SYSTEM 态下 OS 切换。
    """

    theme_changed = Signal()  # 2026-08-30 v1.5.0 PR #A5:主题变化(任意态)

    _current: Theme = Theme.LIGHT

    @classmethod
    def current(cls) -> Theme:
        """当前激活主题(进程级单例 — 由 `apply()` / `toggle()` 维护)。

        注:在 SYSTEM 态下,`current()` 返回 SYSTEM,但 `actual()` 才是实际
        应用的 light/dark。QSS 注入和 nav_bar.refresh_theme() 走 `actual()`
        来拿实际值(避免按 SYSTEM 给 nav_bar 上暗色底)。
        """
        return cls._current

    @classmethod
    def actual(cls) -> Theme:
        """2026-08-30 v1.5.0 PR #A5:实际生效的主题(LIGHT 或 DARK)— SYSTEM 态时
        按 OS 当前 colorScheme 解析。LIGHT/DARK 态时与 current() 一致。
        """
        if cls._current != Theme.SYSTEM:
            return cls._current
        return cls._resolve_system_theme()

    @classmethod
    def _resolve_system_theme(cls) -> Theme:
        """读 QStyleHints.colorScheme() → LIGHT/DARK。

        若 Qt 不可用 / QApplication 未构造 → fallback LIGHT。
        """
        try:
            from PySide6.QtWidgets import QApplication  # noqa: PLC0415

            app = QApplication.instance()
            if app is None:
                return Theme.LIGHT
            scheme = app.styleHints().colorScheme()
            # Qt.ColorScheme.Dark=2 / Light=0 / Unknown=1
            # 直接用 int() 不可(ColorScheme 是 enum 但不是 IntEnum)— 用 .value
            scheme_value = int(scheme.value) if hasattr(scheme, "value") else int(scheme)
            return Theme.DARK if scheme_value == 2 else Theme.LIGHT
        except (ImportError, AttributeError, RuntimeError, ValueError):
            return Theme.LIGHT

    @classmethod
    def load_qss(cls, theme: Theme) -> str:
        """从 `tgmonitor.ui.resources` 加载主题 QSS(DARK→`style_dark.qss`,LIGHT→`style.qss`)。"""
        # 2026-08-30 v1.5.0 PR #A5:SYSTEM 态 → 先解析到 LIGHT/DARK 再 load。
        actual = cls.actual() if theme == Theme.SYSTEM else theme
        if actual == Theme.DARK:
            return (
                resources.files("tgmonitor.ui.resources")
                .joinpath("style_dark.qss")
                .read_text("utf-8")
            )
        return resources.files("tgmonitor.ui.resources").joinpath("style.qss").read_text("utf-8")

    @classmethod
    def apply(cls, theme: Theme) -> None:
        """应用主题到 QApplication + emit theme_changed signal。

        2026-08-30 v1.5.0 PR #A5:SYSTEM 态监听 `colorSchemeChanged`,
        OS 切换深浅色时自动重渲(只在已注册 listener 时有效,避免重复连)。
        """
        from PySide6.QtWidgets import QApplication

        cls._current = theme
        app = QApplication.instance()
        if app is None:
            return
        actual = cls.actual()
        qss = cls.load_qss(actual)
        # 把 {accent} / {accentHover} 占位符替换成实际 hex,让 QSS 走单源
        qss = qss.replace("{accent}", cls.accent()).replace("{accentHover}", cls.accent("hover"))
        app.setStyleSheet(qss)
        # 2026-08-30 PR #A5:SYSTEM 态挂 colorSchemeChanged listener
        # (只在切到 SYSTEM 时连,非 SYSTEM 时不连,避免重复触发)。
        cls._ensure_system_listener()
        # 通知 UI 端(nav_bar / settings_page 等)按新主题重画
        cls._instance().theme_changed.emit()

    _qobject_instance: ThemeManager | None = None

    @classmethod
    def _instance(cls) -> ThemeManager:
        """懒构造一个 ThemeManager QObject 实例 — 仅供 emit theme_changed 用。

        ThemeManager 主要逻辑走 classmethod(`_current` 是 class var),
        只在 emit signal 时需要 QObject 实例。多数调用方持有 MainWindow,
        那里 `self.theme_changed = ThemeManager._instance().theme_changed`。
        """
        if cls._qobject_instance is None:
            cls._qobject_instance = ThemeManager()
        return cls._qobject_instance

    _system_listener_connected = False

    @classmethod
    def _ensure_system_listener(cls) -> None:
        """若 ThemeManager._current == SYSTEM 且 listener 未连 → 连一次。

        Qt 信号是引用,只在 SYSTEM 态时 `actual()` 变化需要重渲;
        LIGHT/DARK 态下 OS 切换不会被本应用感知(QSS 不动)。
        """
        if cls._system_listener_connected:
            return
        if cls._current != Theme.SYSTEM:
            return
        try:
            from PySide6.QtWidgets import QApplication  # noqa: PLC0415

            app = QApplication.instance()
            if app is None:
                return
            app.styleHints().colorSchemeChanged.connect(cls._on_system_scheme_changed)
            cls._system_listener_connected = True
            log.info("ThemeManager: SYSTEM mode — colorSchemeChange listener connected")
        except (ImportError, AttributeError, RuntimeError) as exc:
            log.warning("ThemeManager: cannot hook colorSchemeChanged: %s", exc)

    @classmethod
    def _on_system_scheme_changed(cls, _scheme: object) -> None:
        """2026-08-30 v1.5.0 PR #A5:OS 切深浅色 → 重渲 QSS + emit signal。

        Qt colorSchemeChanged 在 macOS 13+ / Win 11 / Wayland 下都报,
        X11 不报(灰度)— X11 用户保持 LIGHT。
        """
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415

        app = QApplication.instance()
        if app is not None:
            new = cls.actual()
            qss = cls.load_qss(new)
            qss = qss.replace("{accent}", cls.accent()).replace(
                "{accentHover}", cls.accent("hover")
            )
            app.setStyleSheet(qss)
        # 通知 UI 端(nav_bar / settings_page 等)按新主题重画
        cls._instance().theme_changed.emit()

    @classmethod
    def toggle(cls) -> Theme:
        """切换主题:LIGHT ↔ DARK。SYSTEM 态第一次 toggle 落到 LIGHT。

        不循环 3 态(SYSTEM→LIGHT→DARK→SYSTEM)— v1.5.0 提供独立 UI
        控件(setting 页面 Theme 三选一),快捷键 Ctrl+T 只走 LIGHT/DARK
        二选循环;SYSTEM 通过设置面板切换。
        """
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

        2026-08-30 v1.5.0 PR #A5:SYSTEM 态按 actual() 解析后选 accent。
        """
        actual = cls.actual()
        if actual == Theme.DARK:
            return cls.ACCENT_DARK_HOVER if kind == "hover" else cls.ACCENT_DARK
        return cls.ACCENT_LIGHT_HOVER if kind == "hover" else cls.ACCENT_LIGHT
