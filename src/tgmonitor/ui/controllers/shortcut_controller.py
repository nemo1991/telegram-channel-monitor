# mypy: disable-error-code=attr-defined
"""全局快捷键 controller — `_wire_shortcuts` / `reload_shortcuts` + `_ACTION_SLOTS`。

2026-10-09 v1.12.x 从 `MainWindow` 抽提。F 类 94 LOC 整合:
- 14 个 action → slot 工厂表(`_ACTION_SLOTS`,module-level)
- `bind_shortcuts()`:首次启动按 settings 装 14 个 QShortcut + 同步
  `act_show` / `act_quit` 的 setShortcut
- `reload_shortcuts(settings)`:SettingsPage「保存并应用」成功后热重载
  — deleteLater 旧 QShortcut + 按新 settings 重建

设计:plain class + `MainWindowCtx`。slot 工厂仍吃 `MainWindow` 实例
(`lambda mw: lambda: mw._switch_tab(0)`)— 拿 widget 引用走 ctx 已够,
但 action slot 触发的是 MainWindow 公开方法,保留 main_window 引用更
直接(此 controller 是 1.3 5 controller 里唯一保留反向调用的特例,因为
每个 action 对应 MainWindow 一个具体 widget 操作)。

`_ACTION_SLOTS` 表(module-level)从 main_window.py 搬过来集中维护;
`MainWindow._wire_shortcuts` / `reload_shortcuts` 变 thin delegate。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtGui import QKeySequence, QShortcut

from tgmonitor.core.keybinding import DEFAULT_BINDINGS, binding_for

if TYPE_CHECKING:
    from tgmonitor.ui.controllers._base import MainWindowCtx

log = logging.getLogger(__name__)

# 2026-09-07 v1.6.9:快捷键 action → 槽映射(module-level,工厂函数吃 self)。
# `bind_shortcuts` / `reload_shortcuts` 走这里批量绑,避免 14 处重复
# `_bind(action, slot)` 重复样板。
_ACTION_SLOTS: dict[str, Callable[[Any], Callable[[], None]]] = {
    "tab_live": lambda mw: lambda: mw._switch_tab(0),
    "tab_dashboard": lambda mw: lambda: mw._switch_tab(1),
    "tab_channels": lambda mw: lambda: mw._switch_tab(2),
    "tab_media": lambda mw: lambda: mw._switch_tab(3),
    "tab_settings": lambda mw: lambda: mw._switch_tab(4),
    "refresh": lambda mw: mw._on_refresh_channels,
    "search": lambda mw: mw._focus_search,
    "export": lambda mw: mw._on_export,
    "toggle_theme": lambda mw: mw._on_theme_toggle,
    "quit": lambda mw: mw._quit_app,
    "settings": lambda mw: lambda: mw._switch_tab(4),
    "escape": lambda mw: mw._on_global_escape,
    "copy": lambda mw: mw._copy_current_message_text,
    "show_window": lambda mw: mw._show_and_raise,
}


class ShortcutController:
    """全局键盘快捷键 — 首次绑定 + 热重载。

    持有 `self._shortcuts: dict[str, QShortcut]` 用于热替换 —
    `reload_shortcuts(settings)` 由 SettingsPage「保存并应用」成功
    后调用。`act_show` / `act_quit` 是 MainWindow 必建属性(QAction),
    `bind_shortcuts` / `reload_shortcuts` 直接引用,不 hasattr 探针。
    """

    def __init__(self, ctx: MainWindowCtx) -> None:
        self._ctx = ctx
        self._shortcuts: dict[str, QShortcut] = {}

    def bind_shortcuts(self) -> None:
        """首次启动按 settings 装 14 个 QShortcut + 同步 tray QAction。

        旧实现:14 个硬编码 `QShortcut(QKeySequence("..."), self)`,局部
        变量引用,reload 时无法 deleteLater。
        新实现:`_ACTION_SLOTS` 注册 action→slot,`binding_for(action, ...)`
        拿实际 QKeySequence,空 / 非法跳过。`__init__` 阶段 `self.app.settings`
        已就绪,直接读它。
        """
        settings = self._ctx.app.settings
        bindings = {name: getattr(settings, f"key_{name}", "") for name in DEFAULT_BINDINGS}
        self._build_all(bindings, source="initial")

    def reload_shortcuts(self, settings: Any) -> None:
        """SettingsPage「保存并应用」成功后调此热重载快捷键。

        行为:
        1. 旧 `self._shortcuts` 全部 `deleteLater()`(Qt 在下一事件循环
           释放,避免悬空引用)
        2. 按新 settings 重建 QShortcut(action 字典顺序)
        3. 同步 file menu `act_show` / `act_quit` 的 setShortcut

        幂等 — SettingsPage 路径已显式调一次,`SettingsReactionController`
        兜底再调一次无副作用。
        """
        bindings = {name: getattr(settings, f"key_{name}", "") for name in DEFAULT_BINDINGS}
        for sc in self._shortcuts.values():
            sc.setParent(None)
            sc.deleteLater()
        self._shortcuts.clear()
        self._build_all(bindings, source="reload")
        log.info("shortcuts reloaded: %d bindings", len(self._shortcuts))

    def _build_all(self, bindings: dict[str, str], *, source: str) -> None:
        """按 `bindings` 重建 14 个 QShortcut + 同步 tray QAction。"""
        main_window = self._ctx.main_window
        for action, slot_factory in _ACTION_SLOTS.items():
            seq_str = binding_for(action, bindings.get(action, ""))
            seq = QKeySequence(seq_str)
            if seq.isEmpty():
                continue
            sc = QShortcut(seq, main_window)
            sc.activated.connect(slot_factory(main_window))
            self._shortcuts[action] = sc

        # 2026-10-04 plan 阶段 4.2:act_show / act_quit 是 _build_menu 必建
        # 属性,不再 hasattr 探针 — 直接引用,menu 没建好的话这里崩是合理。
        # controller 走 main_window 引用(此 controller 是 1.3 5 controller 里
        # 唯一保留反向调用的特例 — `act_show` / `act_quit` 是 _build_menu 装配
        # 期属性,无 widget 抽象层可走,直接拿)。
        show_seq = QKeySequence(binding_for("show_window", bindings.get("show_window", "")))
        if not show_seq.isEmpty():
            main_window.act_show.setShortcut(show_seq)
        quit_seq = QKeySequence(binding_for("quit", bindings.get("quit", "")))
        if not quit_seq.isEmpty():
            main_window.act_quit.setShortcut(quit_seq)
        log.debug("shortcuts bound: %d (source=%s)", len(self._shortcuts), source)
