"""快捷键解析 + 默认值表 + 冲突检测。

11 个 action 名 + 当前硬编码默认值的 single source of truth。
MainWindow / SettingsPage 都从这里拿默认 — 不重复硬编码。

设计:
- `DEFAULT_BINDINGS` 是 `QKeySequence.toString()` 形式的字符串(就是 Qt
  portable 字符串格式 `Ctrl+R` / `Ctrl+Shift+L` 等)。
- 函数签名只收 / 返 `str`(核心包零 PySide6 依赖,2026-10-04 阶段 2 修)。
  UI 层(`MainWindow._bind_shortcuts` / `SettingsPage._build_keybindings`)
  自行用 `QKeySequence(s)` 包成 Qt 类型给 QShortcut / QKeySequenceEdit 用。
- 空字符串 = 走默认(与 v1.5.4 `key_theme=""` 同语义)。
- `find_duplicates` 给 SettingsPage「保存并应用」 走冲突校验用,大小写
  不敏感(用户手填 `ctrl+r` 与 `Ctrl+R` 视为冲突)。

调用方:
- `MainWindow._bind_shortcuts(settings)`:用 `binding_for(action, settings_value)`
  拿最终字符串,空 / 非法 → 默认;自己 `QKeySequence(s)` wrap 后给 QShortcut。
- `SettingsPage._build_keybindings`:用 `default_for(action)` 设 placeholder
  + 初值;`find_duplicates(edit_values)` 做保存校验。
"""

from __future__ import annotations

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


def parse_key_sequence(s: str) -> str | None:
    """解析 .env 字符串 → 规范化后的字符串。空串 / 无法解析 → None。

    规范化规则(原 `QKeySequence(s).toString()` 的等价行为):
    - 去前后空白
    - `+` 周围的空白全部去掉(`Ctrl + R` → `Ctrl+R`)
    - 大小写归一(Ctrl / ctrl / CTRL → Ctrl,字母键统一大写 R,功能键首字母大写 Tab)
    - 字母 / 功能键大小写归一后,与输入字符串不匹配 → None(原 Qt 的"非空但非法"行为)

    设计:与 Qt `QKeySequence(s).toString() != ""` 等价的语义检查 —
    「解析成功但实际是空」为 false positive,应当 None。
    """
    if not s or not s.strip():
        return None
    norm = _normalize(s)
    if not norm:
        return None
    return norm


def _normalize(s: str) -> str:
    """把 `"Ctrl + R"` / `"ctrl+r"` / `"CTRL+R"` 统一为 `"Ctrl+R"`。

    非空但归一后仍是空(全空白 / 全是无法识别的 token)→ 返空串,
    让 `parse_key_sequence` 视为 None(与 Qt `QKeySequence(s).toString() == ""` 等价)。
    """
    parts = s.split("+")
    tokens: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        norm = _normalize_token(p)
        if not norm:
            return ""  # 任意一个 token 不认识 → 整串无效
        tokens.append(norm)
    if not tokens:
        return ""
    return "+".join(tokens)


# modifier / 功能键的大小写归一表(Qt portable string 用同样的形式)
_MODIFIER_KEYS = {"Ctrl", "Shift", "Alt", "Meta"}
_FUNCTION_KEYS = {
    "Esc",
    "Escape",
    "Tab",
    "Backspace",
    "Return",
    "Enter",
    "Space",
    "Delete",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "Up",
    "Down",
    "Left",
    "Right",
    "Insert",
    "Print",
    "Pause",
    "CapsLock",
    "NumLock",
    "ScrollLock",
}


def _is_known_token(t: str) -> bool:
    """判断单个 token 是否是合法的 Qt portable key 字符串。

    Qt 接受 3 类:
    - modifier key:Ctrl / Shift / Alt / Meta
    - function / 方向 / 特殊键:见 _FUNCTION_KEYS
    - 单字符键:1 个 ASCII 字母或数字(0-9 / a-z / A-Z)
    - F1-F35:前缀 F + 数字

    其他输入(纯符号 / 下划线 / 垃圾字符 / 多字符乱码)→ False,与 Qt
    `QKeySequence(s).toString() == ""` 等价。
    """
    if t in _MODIFIER_KEYS or t.upper() in _MODIFIER_KEYS:
        return True
    if t in _FUNCTION_KEYS or t.upper() in _FUNCTION_KEYS:
        return True
    if len(t) == 1 and t.isalnum():
        return True
    # F 数字(F1-F35)
    if t.upper().startswith("F") and t[1:].isdigit():
        f_num = int(t[1:])
        return 1 <= f_num <= 35
    return False


_MODIFIER_NORMALIZED: dict[str, str] = {
    "Ctrl": "Ctrl",
    "CTRL": "Ctrl",
    "Shift": "Shift",
    "SHIFT": "Shift",
    "Alt": "Alt",
    "ALT": "Alt",
    "Meta": "Meta",
    "META": "Meta",
}
_FUNCTION_NORMALIZED: dict[str, str] = {
    # 标准 case 已是规范形式
    "Esc": "Esc",
    "Escape": "Escape",
    "Tab": "Tab",
    "Backspace": "Backspace",
    "Return": "Return",
    "Enter": "Enter",
    "Space": "Space",
    "Delete": "Delete",
    "Home": "Home",
    "End": "End",
    "PageUp": "PageUp",
    "PageDown": "PageDown",
    "Up": "Up",
    "Down": "Down",
    "Left": "Left",
    "Right": "Right",
    "Insert": "Insert",
    "Print": "Print",
    "Pause": "Pause",
    "CapsLock": "CapsLock",
    "NumLock": "NumLock",
    "ScrollLock": "ScrollLock",
    # 全大写兜底
    "ESC": "Esc",
    "ESCAPE": "Escape",
}


def _normalize_token(t: str) -> str:
    """单个 token(已 strip)的大小写归一;不在已知表 → 返空让上层拒。"""
    if t in _MODIFIER_NORMALIZED:
        return _MODIFIER_NORMALIZED[t]
    if t in _FUNCTION_NORMALIZED:
        return _FUNCTION_NORMALIZED[t]
    if len(t) == 1 and t.isalpha():
        return t.upper()
    if len(t) >= 2 and t[0] in ("F", "f") and t[1:].isdigit():
        f_num = int(t[1:])
        if 1 <= f_num <= 35:
            return f"F{f_num}"
    if len(t) == 1 and t.isdigit():
        return t
    return ""  # 未知 token — 整串 reject


def default_for(action: str) -> str:
    """返回 action 的硬编码默认 key string(action 不存在 → 空串)。"""
    return DEFAULT_BINDINGS.get(action, "")


def binding_for(action: str, settings_value: str) -> str:
    """返回实际生效的 key string。

    优先级:
    1. settings_value 解析成功 → 用 settings_value(归一化后)
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
