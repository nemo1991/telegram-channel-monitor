"""PR #A5 — Theme.SYSTEM 态 + ThemeManager.actual() + accent 解析。

不真触发 `colorSchemeChanged`(Qt offscreen 不报),只验:
- `Theme.SYSTEM` enum 存在
- `current()` 返回 SYSTEM,`actual()` 解析为 LIGHT/DARK
- `accent()` 在 SYSTEM 态按 actual() 选色
- `load_qss()` 在 SYSTEM 态走 LIGHT/DARK qss
- `toggle()` 在 SYSTEM 态首次落到 LIGHT(不循环 3 态)
"""

from __future__ import annotations

import os

# offscreen:跑测试不弹真窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.ui.theme import Theme, ThemeManager  # noqa: E402


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    """构造一次 QApplication — 多次跑 UI 测试不重复创建。"""
    app = QApplication.instance() or QApplication([])
    return app  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def reset_theme() -> None:
    """每个测试前后重置 ThemeManager._current,免得互相污染。"""
    ThemeManager._current = Theme.LIGHT
    ThemeManager._system_listener_connected = False
    yield  # noqa: F401 — fixture
    ThemeManager._current = Theme.LIGHT
    ThemeManager._system_listener_connected = False


def test_theme_enum_has_system() -> None:
    """Theme.SYSTEM 是 enum 成员,value="system"。"""
    assert Theme.SYSTEM.value == "system"
    assert Theme.LIGHT.value == "light"
    assert Theme.DARK.value == "dark"


def test_actual_returns_current_when_not_system(qt_app: QApplication) -> None:
    """LIGHT/DARK 态:actual() == current()。"""
    ThemeManager._current = Theme.LIGHT
    assert ThemeManager.actual() == Theme.LIGHT
    ThemeManager._current = Theme.DARK
    assert ThemeManager.actual() == Theme.DARK


def _make_scheme(value: int) -> MagicMock:
    """构造一个 Qt ColorScheme mock — 有 `.value` 属性。"""
    m = MagicMock()
    m.value = value
    return m


def test_actual_resolves_to_light_in_system_when_app_light(
    qt_app: QApplication,
) -> None:
    """SYSTEM 态 + Qt scheme=Light → actual()=LIGHT。"""
    ThemeManager._current = Theme.SYSTEM
    with patch.object(
        QApplication.instance().styleHints(),
        "colorScheme",
        return_value=_make_scheme(0),  # Light
    ):
        assert ThemeManager.actual() == Theme.LIGHT


def test_actual_resolves_to_dark_in_system_when_app_dark(
    qt_app: QApplication,
) -> None:
    """SYSTEM 态 + Qt scheme=Dark → actual()=DARK。"""
    ThemeManager._current = Theme.SYSTEM
    with patch.object(
        QApplication.instance().styleHints(),
        "colorScheme",
        return_value=_make_scheme(2),  # Dark
    ):
        assert ThemeManager.actual() == Theme.DARK


def test_accent_uses_actual_when_system(qt_app: QApplication) -> None:
    """SYSTEM 态 + OS 暗色 → accent 走 DARK 配色。"""
    ThemeManager._current = Theme.SYSTEM
    with patch.object(
        QApplication.instance().styleHints(),
        "colorScheme",
        return_value=_make_scheme(2),
    ):
        assert ThemeManager.accent() == ThemeManager.ACCENT_DARK
        assert ThemeManager.accent("hover") == ThemeManager.ACCENT_DARK_HOVER


def test_load_qss_system_falls_back_to_dark_qss(
    qt_app: QApplication,
) -> None:
    """SYSTEM 态 + OS 暗色 → load_qss(SYSTEM) 实际返 DARK qss。"""
    ThemeManager._current = Theme.SYSTEM
    with patch.object(
        QApplication.instance().styleHints(),
        "colorScheme",
        return_value=_make_scheme(2),
    ):
        # SYSTEM 传参 → 走 actual()=DARK → DARK qss
        qss = ThemeManager.load_qss(Theme.SYSTEM)
        assert len(qss) > 0
        # 反证 LIGHT qss 不同 — 跑一次 LIGHT 拿比较
        ThemeManager._current = Theme.LIGHT
        light_qss = ThemeManager.load_qss(Theme.LIGHT)
        assert light_qss != qss


def test_toggle_skips_system_in_two_state_cycle(qt_app: QApplication) -> None:
    """Ctrl+T 快捷键走 LIGHT↔DARK 二选循环,SYSTEM 不在循环里。"""
    ThemeManager._current = Theme.LIGHT
    assert ThemeManager.toggle() == Theme.DARK
    assert ThemeManager.toggle() == Theme.LIGHT
    assert ThemeManager.toggle() == Theme.DARK
    # 从 SYSTEM 状态 toggle → 落到 LIGHT(不返 SYSTEM)
    ThemeManager._current = Theme.SYSTEM
    new = ThemeManager.toggle()
    assert new == Theme.LIGHT


def test_apply_emits_theme_changed_signal(qt_app: QApplication) -> None:
    """`apply()` 每次 emit theme_changed signal(UI 端用来刷新 nav_bar 等)。"""
    ThemeManager._current = Theme.LIGHT
    captured: list[int] = []
    ThemeManager._instance().theme_changed.connect(lambda: captured.append(1))
    ThemeManager.apply(Theme.DARK)
    assert len(captured) == 1
    ThemeManager.apply(Theme.LIGHT)
    assert len(captured) == 2


def test_apply_with_system_connects_listener_once(
    qt_app: QApplication,
) -> None:
    """SYSTEM 态 `apply()` 触发 `_ensure_system_listener` 一次连接。"""
    ThemeManager._current = Theme.LIGHT
    ThemeManager._system_listener_connected = False
    ThemeManager.apply(Theme.SYSTEM)
    assert ThemeManager._system_listener_connected is True
    # 重复 apply(SYSTEM) 不重复连接
    ThemeManager.apply(Theme.SYSTEM)
    assert ThemeManager._system_listener_connected is True


def test_apply_with_light_does_not_connect_listener(
    qt_app: QApplication,
) -> None:
    """LIGHT/DARK 态 `apply()` 不挂 colorSchemeChanged listener(避免无意义重渲)。

    验证路径:清 `_system_listener_connected=False` → apply(LIGHT) →
    应保持 False(因为 `_ensure_system_listener` 在非 SYSTEM 态下不会 connect)。
    """
    ThemeManager._current = Theme.LIGHT
    ThemeManager._system_listener_connected = False
    ThemeManager.apply(Theme.LIGHT)
    assert ThemeManager._system_listener_connected is False
    ThemeManager.apply(Theme.DARK)
    assert ThemeManager._system_listener_connected is False


# ============================================================
# 2026-09-03 v1.5.4 PR #P4:主题持久化
# ============================================================


def test_settings_has_key_theme_default_empty(qt_app: QApplication) -> None:
    """PR #P4:`Settings.key_theme` 字段默认空字符串(不持久化,兼容 v1.5.0)。"""
    from tgmonitor.core.config import Settings

    s = Settings(env_file=None)
    assert s.key_theme == ""


def test_settings_key_theme_round_trip_via_env(qt_app: QApplication) -> None:
    """PR #P4:`Settings.key_theme` 走 pydantic-settings env 加载路径。"""
    from tgmonitor.core.config import Settings

    s = Settings(env_file=None, key_theme="dark")
    assert s.key_theme == "dark"


def test_editable_settings_key_theme_round_trip(qt_app: QApplication) -> None:
    """PR #P4:EditableSettings.key_theme 字段 + from_settings / to_settings 透传。"""
    from tgmonitor.core.config import Settings
    from tgmonitor.core.settings_store import EditableSettings

    s = Settings(env_file=None, key_theme="light")
    e = EditableSettings.from_settings(s)
    assert e.key_theme == "light"
    # to_settings 透传
    s2 = e.to_settings()
    assert s2.key_theme == "light"


def test_editable_settings_key_theme_default_empty(qt_app: QApplication) -> None:
    """PR #P4:EditableSettings 默认 key_theme 空字符串(与 Settings 对齐)。"""
    from tgmonitor.core.settings_store import EditableSettings

    e = EditableSettings()
    assert e.key_theme == ""
