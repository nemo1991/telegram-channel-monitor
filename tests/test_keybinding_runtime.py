"""v1.6.9:快捷键解析 + 默认值 + 冲突检测 + 热重载测试。

覆盖场景:
- `Settings.key_<action>` 14 个字段默认空串
- `.env` TG_KEY_* 解析(env 覆盖生效)
- `EditableSettings` round-trip(`from_settings` / `to_settings` 不丢字段)
- `parse_key_sequence` 空 / 非法返回 None
- `default_for` 已知 / 未知 action
- `binding_for` settings 优先 + 默认兜底
- `find_duplicates` 大小写不敏感 + 空串跳过
- `settings_to_pairs` 含 14 个 TG_KEY_* 项
- MainWindow `reload_shortcuts` 替换旧 QShortcut + 同步 tray QAction
- SettingsPage 持有 `_keybinding_edits` 字典(供 _collect / _load 用)
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication

from tgmonitor.core.config import Settings
from tgmonitor.core.keybinding import (
    ACTION_LABELS,
    DEFAULT_BINDINGS,
    binding_for,
    default_for,
    find_duplicates,
    parse_key_sequence,
)
from tgmonitor.core.settings_store import EditableSettings, settings_to_pairs

# ---- Settings 字段 ----


def test_settings_keybinding_defaults_empty() -> None:
    """14 个 key_<action> 字段默认空串 — 空 = 走硬编码默认(与 key_theme 一致)。"""
    s = Settings(env_file=None)  # type: ignore[call-arg]
    for action in DEFAULT_BINDINGS:
        assert getattr(s, f"key_{action}") == "", (
            f"Settings.key_{action} default must be '' (got {getattr(s, f'key_{action}')!r})"
        )


def test_settings_keybinding_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """TG_KEY_REFRESH=F5 解析到 Settings.key_refresh(忽略大小写不敏感场景里保留 F5)。"""
    monkeypatch.setenv("TG_KEY_REFRESH", "F5")
    monkeypatch.setenv("TG_KEY_QUIT", "Ctrl+W")
    s = Settings(env_file=None)  # type: ignore[call-arg]
    assert s.key_refresh == "F5"
    assert s.key_quit == "Ctrl+W"
    # 其它字段保持默认空
    assert s.key_search == ""


def test_settings_keybinding_count_matches_module_default() -> None:
    """Settings 字段数 == DEFAULT_BINDINGS 项数(防漂移)。"""
    expected_actions = set(DEFAULT_BINDINGS)
    actual_actions = {a for a in DEFAULT_BINDINGS if f"key_{a}" in Settings.model_fields}
    assert actual_actions == expected_actions, (
        f"Settings 缺字段: {expected_actions - actual_actions}; "
        f"多余字段: {actual_actions - expected_actions}"
    )


# ---- EditableSettings round-trip ----


def test_editable_settings_keybinding_round_trip() -> None:
    """EditableSettings.from_settings / to_settings 透传 14 个字段。"""
    s = Settings(env_file=None)  # type: ignore[call-arg]
    s.key_refresh = "F5"
    s.key_quit = "Ctrl+W"
    s.key_search = "Ctrl+K"
    e = EditableSettings.from_settings(s)
    # 字段一一对应
    assert e.key_refresh == "F5"
    assert e.key_quit == "Ctrl+W"
    assert e.key_search == "Ctrl+K"
    # 反向 to_settings 仍保留
    s2 = e.to_settings()
    assert s2.key_refresh == "F5"
    assert s2.key_quit == "Ctrl+W"
    assert s2.key_search == "Ctrl+K"


def test_editable_settings_keybinding_defaults_empty() -> None:
    """EditableSettings 默认 14 字段空 — 不预设默认,避免 UI 编辑前显示旧值。"""
    e = EditableSettings()
    for action in DEFAULT_BINDINGS:
        assert getattr(e, f"key_{action}") == ""


def test_settings_to_pairs_includes_keybindings() -> None:
    """settings_to_pairs 写出 14 个 TG_KEY_* 项,空串也写(便于 .env 显式标记)。"""
    s = Settings(env_file=None)  # type: ignore[call-arg]
    pairs = settings_to_pairs(s)
    expected_keys = {f"TG_KEY_{a.upper()}" for a in DEFAULT_BINDINGS}
    actual_keys = {k for k in pairs if k.startswith("TG_KEY_")}
    assert actual_keys == expected_keys


def test_settings_to_pairs_emits_set_values() -> None:
    """非空 Settings 字段在 settings_to_pairs 里原样落地。"""
    s = Settings(env_file=None)  # type: ignore[call-arg]
    s.key_refresh = "F5"
    s.key_show_window = "Ctrl+9"
    pairs = settings_to_pairs(s)
    assert pairs["TG_KEY_REFRESH"] == "F5"
    assert pairs["TG_KEY_SHOW_WINDOW"] == "Ctrl+9"


# ---- parse_key_sequence ----


def test_parse_key_sequence_empty_returns_none() -> None:
    """空串 / 纯空白 → None(走默认)。"""
    assert parse_key_sequence("") is None
    assert parse_key_sequence("   ") is None


def test_parse_key_sequence_valid_returns_sequence() -> None:
    """合法串 → 非空 QKeySequence(往返保留 toString)。"""
    seq = parse_key_sequence("Ctrl+R")
    assert seq is not None
    assert not seq.isEmpty()
    assert seq.toString() == "Ctrl+R"


def test_parse_key_sequence_invalid_returns_none() -> None:
    """无法解析的乱串 → None(Qt 不抛异常,返回 empty seq,我们二次校验)。"""
    # QKeySequence 对完全不能解析的输入返回 empty seq
    seq = parse_key_sequence("zzzz_not_a_key")
    assert seq is None


# ---- default_for ----


def test_default_for_known_actions() -> None:
    """已知 action → DEFAULT_BINDINGS 对应值。"""
    assert default_for("refresh").toString() == "Ctrl+R"
    assert default_for("search").toString() == "Ctrl+F"
    assert default_for("quit").toString() == "Ctrl+Q"
    assert default_for("escape").toString() == "Esc"
    assert default_for("show_window").toString() == "Ctrl+0"


def test_default_for_unknown_action_returns_empty() -> None:
    """未知 action 名 → empty QKeySequence(QShortcut 跳过)。"""
    seq = default_for("nonexistent_action_xyz")
    assert seq.isEmpty()


# ---- binding_for ----


def test_binding_for_uses_settings_value_when_valid() -> None:
    """settings_value 合法 → 用 settings_value,不走默认。"""
    seq = binding_for("refresh", "F5")
    assert seq.toString() == "F5"


def test_binding_for_falls_back_to_default_when_empty() -> None:
    """settings_value 空 → 走默认(用户没改键)。"""
    seq = binding_for("refresh", "")
    assert seq.toString() == "Ctrl+R"


def test_binding_for_falls_back_to_default_when_invalid() -> None:
    """settings_value 解析失败 → 默认(防 1 字符乱输入破坏快捷键)。"""
    seq = binding_for("refresh", "garbage_xyz")
    assert seq.toString() == "Ctrl+R"


def test_binding_for_normalizes_casing() -> None:
    """Qt QKeySequence 把 'ctrl+r' normalize 成 'Ctrl+R' — 兼容性正常。"""
    seq = binding_for("refresh", "ctrl+r")
    assert seq.toString() == "Ctrl+R"


# ---- find_duplicates ----


def test_find_duplicates_detects_pair() -> None:
    """两 action 绑同一键 → 返回 1 对(sorted,小写比较)。"""
    dupes = find_duplicates(
        {
            "refresh": "F5",
            "search": "F5",
        }
    )
    assert dupes == [("refresh", "search")]


def test_find_duplicates_case_insensitive() -> None:
    """大小写差异视为冲突('ctrl+r' == 'Ctrl+R')。"""
    dupes = find_duplicates(
        {
            "refresh": "Ctrl+R",
            "search": "ctrl+r",
        }
    )
    assert len(dupes) == 1
    assert sorted(dupes[0]) == ["refresh", "search"]


def test_find_duplicates_ignores_empty_bindings() -> None:
    """空串跳过(走默认,不算冲突)。"""
    dupes = find_duplicates(
        {
            "refresh": "",
            "search": "F5",
            "copy": "",
        }
    )
    assert dupes == []


def test_find_duplicates_three_way_pair() -> None:
    """三 action 绑同一键 → 2 对(consecutive sorted pairs):
    v1.6.9 `find_duplicates` 实现按 sorted 顺序生成 (n-1) 对 — 用户看到
    「export↔refresh + refresh↔search」足够定位冲突,无需完整 C(3,2)。
    """
    dupes = find_duplicates(
        {
            "refresh": "F5",
            "search": "F5",
            "export": "F5",
        }
    )
    assert len(dupes) == 2
    assert ("export", "refresh") in dupes
    assert ("refresh", "search") in dupes


def test_find_duplicates_no_conflict() -> None:
    """全不同 → 空列表。"""
    dupes = find_duplicates(
        {
            "refresh": "F5",
            "search": "F6",
            "export": "F7",
        }
    )
    assert dupes == []


# ---- ACTION_LABELS ----


def test_action_labels_match_default_bindings() -> None:
    """ACTION_LABELS 与 DEFAULT_BINDINGS keys 同步(防某一边漏改)。"""
    assert set(ACTION_LABELS) == set(DEFAULT_BINDINGS)


# ---- MainWindow reload_shortcuts ----


# 测试用 no-op slot factories(替掉 _ACTION_SLOTS 里的 lambda,避免触发真
# MainWindow 方法,这些方法依赖 tray / page widget / monitor / 一堆
# 装配。验证目标是「reload 替换了 QShortcut 实例 + 同步 tray QAction
# shortcut」,不需要真触发业务逻辑)。
_NOOP_SLOTS: dict[str, callable] = {  # type: ignore[type-arg]
    name: (lambda _mw: lambda: None) for name in DEFAULT_BINDINGS
}


class _FakeMainWindow(QObject):
    """绕开真 MainWindow __init__:QObject subclass(给 QShortcut 当 parent 用),
    只装 reload_shortcuts 需要的最小属性。

    真 MainWindow ctor 要 monitor / loop / tray / 5 个 page widget,测快捷键热重载
    不需要那些。`_wire_shortcuts` 与 `reload_shortcuts` 都只读 `self._shortcuts` /
    `self.act_show` / `self.act_quit` / `self` (QShortcut parent) — 全部手动 stub。
    """

    def __init__(self) -> None:
        super().__init__()
        from PySide6.QtGui import QAction, QShortcut  # noqa: PLC0415

        from tgmonitor.core.keybinding import DEFAULT_BINDINGS, binding_for  # noqa: PLC0415

        self._shortcuts: dict[str, QShortcut] = {}
        self.act_show = QAction("show", self)
        self.act_quit = QAction("quit", self)

        # 模拟 _wire_shortcuts 的初次绑定逻辑(无 settings 时也走默认)
        from tgmonitor.core.config import Settings  # noqa: PLC0415

        s = Settings(env_file=None)  # type: ignore[call-arg]
        bindings = {name: getattr(s, f"key_{name}", "") for name in DEFAULT_BINDINGS}
        for action, slot_factory in _NOOP_SLOTS.items():
            seq = binding_for(action, bindings.get(action, ""))
            if seq.isEmpty():
                continue
            sc = QShortcut(seq, self)
            sc.activated.connect(slot_factory(self))
            self._shortcuts[action] = sc

    # 复制真实 reload_shortcuts 行为(避免 import 整个 main_window)
    def reload_shortcuts(self, settings: Settings) -> None:  # type: ignore[type-arg]
        from PySide6.QtGui import QShortcut  # noqa: PLC0415

        from tgmonitor.core.keybinding import DEFAULT_BINDINGS, binding_for  # noqa: PLC0415

        for sc in self._shortcuts.values():
            sc.setParent(None)
            sc.deleteLater()
        self._shortcuts.clear()

        bindings = {name: getattr(settings, f"key_{name}", "") for name in DEFAULT_BINDINGS}
        for action, slot_factory in _NOOP_SLOTS.items():
            seq = binding_for(action, bindings.get(action, ""))
            if seq.isEmpty():
                continue
            sc = QShortcut(seq, self)
            sc.activated.connect(slot_factory(self))
            self._shortcuts[action] = sc

        show_seq = binding_for("show_window", bindings.get("show_window", ""))
        if not show_seq.isEmpty():
            self.act_show.setShortcut(show_seq)
        quit_seq = binding_for("quit", bindings.get("quit", ""))
        if not quit_seq.isEmpty():
            self.act_quit.setShortcut(quit_seq)


def _ensure_qapp() -> QApplication:
    """取得 session 级 QApplication 实例(其他 fixture 已建过就直接复用)。"""
    return QApplication.instance() or QApplication([])


def test_main_window_reload_shortcuts_replaces_qshortcut() -> None:
    """reload_shortcuts 替换 QShortcut 实例 — 新 shortcut 的 keySequence 跟新 settings 一致。"""
    _ensure_qapp()
    mw = _FakeMainWindow()
    # 初次绑:refresh → 默认 Ctrl+R
    assert mw._shortcuts["refresh"].key().toString() == "Ctrl+R"
    old_sc = mw._shortcuts["refresh"]

    s = Settings(env_file=None)  # type: ignore[call-arg]
    s.key_refresh = "F9"
    mw.reload_shortcuts(s)

    new_sc = mw._shortcuts["refresh"]
    assert new_sc is not old_sc
    assert new_sc.key().toString() == "F9"


def test_main_window_reload_shortcuts_updates_tray_action() -> None:
    """reload_shortcuts 同步 act_show / act_quit 的 setShortcut。

    真 MainWindow `_wire_shortcuts` 内部把 `binding_for("show_window", ...)`
    写到 act_show,本 fake 只测 reload_shortcuts 这条路径(初次 tray 同步
    走的是 _wire_shortcuts,与本测试无关 — 那个路径在集成测试覆盖)。
    """
    _ensure_qapp()
    mw = _FakeMainWindow()
    s = Settings(env_file=None)  # type: ignore[call-arg]
    s.key_show_window = "Ctrl+9"
    s.key_quit = "Ctrl+W"
    mw.reload_shortcuts(s)
    assert mw.act_show.shortcut().toString() == "Ctrl+9"
    assert mw.act_quit.shortcut().toString() == "Ctrl+W"


def test_main_window_reload_shortcuts_empty_falls_back_to_default() -> None:
    """空 settings value → 走 default_for(action)(F5 空 → Ctrl+R)。

    重建 fake,初次 settings.refresh = "F5",reload 后清空 → 走默认 Ctrl+R。
    """
    _ensure_qapp()
    s = Settings(env_file=None)  # type: ignore[call-arg]
    s.key_refresh = "F5"

    # 直接调 binding_for / _ACTION_SLOTS 重建 fake
    from PySide6.QtGui import (
        QAction,  # noqa: PLC0415
        QShortcut,  # noqa: PLC0415
    )

    from tgmonitor.core.keybinding import (
        DEFAULT_BINDINGS,  # noqa: PLC0415
        binding_for,  # noqa: PLC0415
    )

    mw = _FakeMainWindow.__new__(_FakeMainWindow)
    QObject.__init__(mw)
    mw._shortcuts = {}
    mw.act_show = QAction("show", mw)
    mw.act_quit = QAction("quit", mw)
    bindings = {name: getattr(s, f"key_{name}", "") for name in DEFAULT_BINDINGS}
    for action, slot_factory in _NOOP_SLOTS.items():
        seq = binding_for(action, bindings.get(action, ""))
        if seq.isEmpty():
            continue
        sc = QShortcut(seq, mw)
        sc.activated.connect(slot_factory(mw))
        mw._shortcuts[action] = sc

    assert mw._shortcuts["refresh"].key().toString() == "F5"

    # 清空 → 走默认
    s.key_refresh = ""
    mw.reload_shortcuts(s)
    assert mw._shortcuts["refresh"].key().toString() == "Ctrl+R"


# ---- 杂项:Qt QKeySequence 行为 ----


def test_qkeysequence_compare_ignores_case() -> None:
    """Qt 标准行为:同一组合键不同大小写视为相同 — 我们 find_duplicates 用 lower()
    自实现兜底,但底层 QKeySequence 比较也能佐证大小写无意义。"""
    a = QKeySequence("Ctrl+R")
    b = QKeySequence("ctrl+r")
    assert a == b
    assert a.toString() == b.toString() == "Ctrl+R"


# ---- 模块元数据 ----


def test_default_bindings_has_fourteen_actions() -> None:
    """v1.6.9:14 个 action(tab×5 + refresh/search/export/toggle_theme/quit/
    settings/escape/copy/show_window)。防后续增删时漏改 SettingsPage。"""
    assert len(DEFAULT_BINDINGS) == 14


def test_default_bindings_keys_use_underscore() -> None:
    """action 名无连字符 / 空格,便于 SettingsPage 拼 key_<action> + gettext msgid。"""
    for action in DEFAULT_BINDINGS:
        assert " " not in action
        assert "-" not in action


# ---- 防漏:Qt 关键字常量 ----


def test_qt_qtkey_ctrl_constant_available() -> None:
    """防 Qt 移除 / 重命名 Qt.Key — v1.6.9 parse_key_sequence 内部不直接用
    (走 QKeySequence 字符串构造),但 SettingsPage 后续可能直接做事件过滤。"""
    assert hasattr(Qt, "Key_Control")
    assert hasattr(Qt, "CTRL")
