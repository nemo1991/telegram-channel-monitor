"""v1.6.9:快捷键解析 + 默认值表 + 冲突检测。

11 个 action 名 + 当前硬编码默认值的 single source of truth。
MainWindow / SettingsPage 都从这里拿默认 — 不重复硬编码。

设计:
- `DEFAULT_BINDINGS` 是 QKeySequence.toString() 形式的字符串,直接给
  `QKeySequence(s)` 用(Qt portable 字符串格式)。
- 空字符串 = 走默认(与 v1.5.4 `key_theme=""` 同语义)。
- `find_duplicates` 给 SettingsPage「保存并应用」 走冲突校验用,大小写
  不敏感(用户手填 `ctrl+r` 与 `Ctrl+R` 视为冲突)。

调用方:
- `MainWindow._bind_shortcuts(settings)`:用 `binding_for(action, settings_value)`
  拿最终 QKeySequence,空 / 非法 → 默认。
- `SettingsPage._build_keybindings`:用 `default_for(action)` 设 placeholder
  + 初值;`find_duplicates(edit_values)` 做保存校验。
"""

from __future__ import annotations

from PySide6.QtGui import QKeySequence

# action -> 默认 QKeySequence 字符串(就是 MainWindow 当前硬编码值)
DEFAULT_BINDINGS: dict[str, str] = {
    "tab_live": "Ctrl+1",
    "tab_dashboard": "Ctrl+2",
    "tab_channels": "Ctrl+3",
    "tab_media": "Ctrl+4",
    "tab_settings": "Ctrl+5",
    "refresh": "Ctrl+R",
    "search": "Ctrl+F",
    "export": "Ctrl+E",
    "toggle_theme": "Ctrl+T",
    "quit": "Ctrl+Q",
    "settings": "Ctrl+,",
    "escape": "Esc",
    "copy": "Ctrl+C",
    "show_window": "Ctrl+0",
}

# action -> 用户可读英文 label;tr() 在 SettingsPage 渲染时调
ACTION_LABELS: dict[str, str] = {
    "tab_live": "Switch to LIVE tab",
    "tab_dashboard": "Switch to DASHBOARD tab",
    "tab_channels": "Switch to CHANNELS tab",
    "tab_media": "Switch to MEDIA tab",
    "tab_settings": "Switch to SETTINGS tab",
    "refresh": "Refresh channel list",
    "search": "Focus search bar",
    "export": "Export current view",
    "toggle_theme": "Toggle theme",
    "quit": "Quit",
    "settings": "Open settings",
    "escape": "Global escape",
    "copy": "Copy current message",
    "show_window": "Show main window",
}


def parse_key_sequence(s: str) -> QKeySequence | None:
    """解析 .env 字符串 → QKeySequence。空串 / 无法解析 → None(走默认)。

    Qt 的 `QKeySequence(garbage)` 不会抛异常,而是返回 empty seq(`isEmpty()` True),
    所以我们要二次校验 `toString() != ""`,防止「解析成功但实际是空」false positive。
    """
    if not s or not s.strip():
        return None
    seq = QKeySequence(s)
    if seq.isEmpty() or seq.toString() == "":
        return None
    return seq


def default_for(action: str) -> QKeySequence:
    """返回 action 的硬编码默认 QKeySequence(action 不存在 → 空 seq)。"""
    s = DEFAULT_BINDINGS.get(action, "")
    return QKeySequence(s) if s else QKeySequence()


def binding_for(action: str, settings_value: str) -> QKeySequence:
    """返回实际生效的 QKeySequence。

    优先级:
    1. settings_value 解析成功 → 用 settings_value
    2. 解析失败 / 空串 → 用 `default_for(action)`
    """
    parsed = parse_key_sequence(settings_value)
    if parsed is not None:
        return parsed
    return default_for(action)


def find_duplicates(
    bindings: dict[str, str],
) -> list[tuple[str, str]]:
    """返回绑了相同 key 字符串的 (action_a, action_b) 对(按 action 名排序)。

    SettingsPage「保存并应用」走校验 → 弹错误 toast,保留旧值。
    大小写不敏感(用户手填 `ctrl+r` 与 `Ctrl+R` 视为冲突)。
    空串跳过(走默认,不算冲突)。
    """
    seen: dict[str, list[str]] = {}
    for action, value in bindings.items():
        if not value or not value.strip():
            continue
        seen.setdefault(value.strip().lower(), []).append(action)
    dupes: list[tuple[str, str]] = []
    for actions in seen.values():
        if len(actions) > 1:
            actions_sorted = sorted(actions)
            for i in range(len(actions_sorted) - 1):
                dupes.append((actions_sorted[i], actions_sorted[i + 1]))
    return dupes
