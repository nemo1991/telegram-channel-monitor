"""`ShortcutController` 单元测试 — 2026-10-09 v1.12.x 下一 plan 目标 1.3。

覆盖:
- `bind_shortcuts` 首次绑定 14 个 QShortcut
- `reload_shortcuts` 替换旧实例 + 同步 act_show/act_quit
- 空 settings 走默认(`binding_for` 兜底)

`_ACTION_SLOTS` 表(module-level)不可直接断言(无对外契约),通过实际
绑定结果验证:`self._shortcuts` dict 的 key 集合 = `DEFAULT_BINDINGS`。
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtGui import QAction, QShortcut  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.core.config import Settings  # noqa: E402
from tgmonitor.core.keybinding import DEFAULT_BINDINGS  # noqa: E402
from tgmonitor.ui.controllers.shortcut_controller import ShortcutController  # noqa: E402
from tgmonitor.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="session")
def qapp_session() -> QApplication:
    """session 级 QApplication(其他 fixture 已建过就直接复用,跟
    `test_keybinding_runtime.py:_ensure_qapp` 同款兜底)。"""
    return QApplication.instance() or QApplication([])


class _FakeMainWindow(QObject):
    """最小 MainWindow 替身 — QObject subclass(给 QShortcut 当 parent 用)。

    走真 QShortcut / QKeySequence(走 Qt 信号/槽机制),但绕过完整 MainWindow
    __init__(避免 5 page widget + tray 装配)。`_ACTION_SLOTS` 工厂还会调
    `mw._on_refresh_channels` / `mw._switch_tab` 等方法,这些是测试桩
    不关心的细节,占位 no-op 即可。
    """

    def __init__(self, qapp: QApplication) -> None:
        super().__init__(qapp)
        self.act_show = QAction("show", self)
        self.act_quit = QAction("quit", self)
        # _ACTION_SLOTS 工厂会调这些方法 — 都给 no-op 占位,避免 AttributeError
        self._on_refresh_channels = lambda: None
        self._focus_search = lambda: None
        self._on_export = lambda: None
        self._on_theme_toggle = lambda: None
        self._quit_app = lambda: None
        self._switch_tab = lambda idx: None
        self._on_global_escape = lambda: None
        self._copy_current_message_text = lambda: None
        self._show_and_raise = lambda: None


def _make_ctx(qapp: QApplication) -> tuple[Any, _FakeMainWindow]:
    """构造 ShortcutController 用的最小 ctx + 拿 main_window 引用。"""
    main_window = _FakeMainWindow(qapp)
    app = MagicMock(name="app")
    app.settings = Settings.for_test()
    ctx = MagicMock(name="ctx", app=app, main_window=main_window)
    return ctx, main_window


class TestBindShortcuts:
    """`bind_shortcuts` 首次启动绑定 14 个 QShortcut。"""

    def test_creates_shortcut_for_every_default_action(self, qapp_session) -> None:
        ctx, mw = _make_ctx(qapp_session)
        ctrl = ShortcutController(ctx)

        ctrl.bind_shortcuts()

        # 14 个 action 都应有 QShortcut(`_ACTION_SLOTS` 14 项 + 都非空 binding)
        assert set(ctrl._shortcuts) == set(DEFAULT_BINDINGS)
        for action in DEFAULT_BINDINGS:
            assert isinstance(ctrl._shortcuts[action], QShortcut)

    def test_binds_to_main_window_as_parent(self, qapp_session) -> None:
        ctx, mw = _make_ctx(qapp_session)
        ctrl = ShortcutController(ctx)

        ctrl.bind_shortcuts()

        # QShortcut.parent 应是 main_window(本身是 QObject)
        for sc in ctrl._shortcuts.values():
            assert sc.parent() is mw

    def test_syncs_act_show_and_act_quit_shortcuts(self, qapp_session) -> None:
        """bind 时同步 act_show / act_quit 的 setShortcut(走 Settings 默认)。"""
        ctx, mw = _make_ctx(qapp_session)
        ctrl = ShortcutController(ctx)

        ctrl.bind_shortcuts()

        # Settings 默认:show_window=Ctrl+0, quit=Ctrl+Q
        assert mw.act_show.shortcut().toString() == "Ctrl+0"
        assert mw.act_quit.shortcut().toString() == "Ctrl+Q"

    def test_uses_settings_key_overrides(self, qapp_session) -> None:
        """Settings.key_<action> 非空时优先用 settings,空时走默认。"""
        ctx, mw = _make_ctx(qapp_session)
        ctx.app.settings.key_refresh = "F9"
        ctx.app.settings.key_search = "Ctrl+K"
        ctrl = ShortcutController(ctx)

        ctrl.bind_shortcuts()

        assert ctrl._shortcuts["refresh"].key().toString() == "F9"
        assert ctrl._shortcuts["search"].key().toString() == "Ctrl+K"
        # 没改的走默认
        assert ctrl._shortcuts["quit"].key().toString() == "Ctrl+Q"

    def test_empty_binding_falls_back_to_default(self, qapp_session) -> None:
        """空 binding → `binding_for` 兜底返 default,不会产生空 QShortcut。"""
        ctx, mw = _make_ctx(qapp_session)
        ctx.app.settings.key_quit = ""
        ctrl = ShortcutController(ctx)

        ctrl.bind_shortcuts()

        # 即便 settings 为空,quit 仍应绑 default(Ctrl+Q),非空 → 不 skip
        assert "quit" in ctrl._shortcuts
        assert ctrl._shortcuts["quit"].key().toString() == "Ctrl+Q"


class TestReloadShortcuts:
    """`reload_shortcuts(settings)` — 热重载,替换 QShortcut + 同步 tray action。"""

    def test_replaces_existing_qshortcut_instances(self, qapp_session) -> None:
        """reload 后 dict 里是新 QShortcut,旧实例应被 deleteLater。"""
        ctx, mw = _make_ctx(qapp_session)
        ctrl = ShortcutController(ctx)
        ctrl.bind_shortcuts()
        old_refresh = ctrl._shortcuts["refresh"]

        s = Settings.for_test()
        s.key_refresh = "F9"
        ctrl.reload_shortcuts(s)

        new_refresh = ctrl._shortcuts["refresh"]
        assert new_refresh is not old_refresh
        assert new_refresh.key().toString() == "F9"

    def test_updates_act_show_shortcut(self, qapp_session) -> None:
        ctx, mw = _make_ctx(qapp_session)
        ctrl = ShortcutController(ctx)
        ctrl.bind_shortcuts()

        s = Settings.for_test()
        s.key_show_window = "Ctrl+9"
        ctrl.reload_shortcuts(s)

        assert mw.act_show.shortcut().toString() == "Ctrl+9"

    def test_updates_act_quit_shortcut(self, qapp_session) -> None:
        ctx, mw = _make_ctx(qapp_session)
        ctrl = ShortcutController(ctx)
        ctrl.bind_shortcuts()

        s = Settings.for_test()
        s.key_quit = "Ctrl+W"
        ctrl.reload_shortcuts(s)

        assert mw.act_quit.shortcut().toString() == "Ctrl+W"

    def test_idempotent_on_same_settings(self, qapp_session) -> None:
        """同 settings 连调两次 reload 不应抛 — 第二次后 dict 仍合法。"""
        ctx, mw = _make_ctx(qapp_session)
        ctrl = ShortcutController(ctx)
        ctrl.bind_shortcuts()

        s = Settings.for_test()
        ctrl.reload_shortcuts(s)
        ctrl.reload_shortcuts(s)  # 第二次幂等

        assert set(ctrl._shortcuts) == set(DEFAULT_BINDINGS)

    def test_empty_setting_falls_back_to_default(self, qapp_session) -> None:
        """reload 时把已绑的 key 清空 → 走 binding_for 默认。"""
        ctx, mw = _make_ctx(qapp_session)
        ctx.app.settings.key_refresh = "F5"
        ctrl = ShortcutController(ctx)
        ctrl.bind_shortcuts()
        assert ctrl._shortcuts["refresh"].key().toString() == "F5"

        s = Settings.for_test()
        s.key_refresh = ""  # 清空
        ctrl.reload_shortcuts(s)

        # 走默认 Ctrl+R
        assert ctrl._shortcuts["refresh"].key().toString() == "Ctrl+R"


class TestActionSlotsTable:
    """`_ACTION_SLOTS` 表覆盖度 — 防后续新增 action 漏注册。"""

    def test_action_slots_cover_all_default_bindings(self) -> None:
        """`_ACTION_SLOTS` 的 key 集合 = `DEFAULT_BINDINGS`(14 个一一对应)。"""
        from tgmonitor.ui.controllers.shortcut_controller import _ACTION_SLOTS

        assert set(_ACTION_SLOTS) == set(DEFAULT_BINDINGS)
        assert len(_ACTION_SLOTS) == 14

    def test_action_slots_are_slot_factories(self, qapp_session) -> None:
        """每个 entry 是 `lambda mw: lambda: ...` 工厂,返 no-arg callable。

        `mw` 走 fake MainWindow(no-op 占位方法)— 工厂会调 `mw._on_X`
        验 callable 但不触发真业务。
        """
        from tgmonitor.ui.controllers.shortcut_controller import _ACTION_SLOTS

        mw = _FakeMainWindow(qapp_session)
        for _action, factory in _ACTION_SLOTS.items():
            slot = factory(mw)
            assert callable(slot)


# ---- 杂项 ----


def test_main_window_thin_delegates_preserved() -> None:
    """2026-10-09:thin delegate `MainWindow._wire_shortcuts` / `reload_shortcuts`
    必须保留方法名 — 旧测试 / SettingsReactionController 走 `mw.reload_shortcuts(...)`
    仍要能 work。
    """
    assert hasattr(MainWindow, "_wire_shortcuts")
    assert hasattr(MainWindow, "reload_shortcuts")
