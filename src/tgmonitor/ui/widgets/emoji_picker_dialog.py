"""2026-09-11 v1.7.4:emoji picker grid — 取代 `QInputDialog.getText` 输入 emoji。

设计要点:
- 60+ emoji 按 7 类(笑脸 / 否定 / 爱 / 手势 / 动物 / 食物 / 活动)分组。
- QGridLayout 8 列 grid,QPushButton 每格;QButtonGroup 互斥 + 高亮选中。
- 「大表情」QCheckBox — 切换 `is_big`(TDLib `reactionTypeEmoji.is_big`)。
- QLineEdit 手输兜底:非预置字符 / custom emoji id(`custom_emoji_id:123`)。
- 静态 `get_emoji(parent)` 入口 — 仿 `QInputDialog.getText`,单选取消返 None。

平台注意:Linux headless 字体可能 tofu,但 picker 字符是字符本身
(不是图片),被文本渲染不影响业务(selection 返回原字符串)。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# 2026-09-11 v1.7.4:7 类 emoji 字符表 — 选 Linux / Win / macOS 字体覆盖度高的常用集合。
_EMOJI_GROUPS: list[tuple[str, list[str]]] = [
    (
        "笑脸",
        [
            "👍",
            "👎",
            "❤️",
            "🔥",
            "🎉",
            "😱",
            "🤩",
            "😍",
            "😂",
            "🤣",
            "🥳",
            "😎",
            "🤔",
            "😢",
            "😭",
            "😡",
            "🤬",
            "🙄",
            "😴",
            "🤯",
        ],
    ),
    (
        "否定",
        ["🙅", "🚫", "❌", "⛔", "🚷", "🤐", "😶", "🤡", "💩", "👻"],
    ),
    (
        "爱/赞",
        [
            "❤️",
            "🧡",
            "💛",
            "💚",
            "💙",
            "💜",
            "🖤",
            "🤍",
            "🤎",
            "💖",
            "💕",
            "💗",
            "💝",
            "💘",
            "💞",
            "💓",
        ],
    ),
    (
        "手势",
        ["👌", "✌️", "🤞", "🤝", "🤲", "🙌", "👏", "🙏", "💪", "🫶"],
    ),
    (
        "动物",
        [
            "🐶",
            "🐱",
            "🐭",
            "🐹",
            "🐰",
            "🦊",
            "🐻",
            "🐼",
            "🐨",
            "🐯",
            "🦁",
            "🐮",
            "🐷",
            "🐸",
            "🐵",
            "🐔",
        ],
    ),
    (
        "食物",
        [
            "🍎",
            "🍊",
            "🍌",
            "🍇",
            "🍓",
            "🍒",
            "🍑",
            "🍍",
            "🥑",
            "🍔",
            "🍕",
            "🍣",
            "🍰",
            "🍩",
            "🍪",
            "🍫",
        ],
    ),
    (
        "活动",
        [
            "⚽",
            "🏀",
            "🏈",
            "⚾",
            "🎾",
            "🏐",
            "🏉",
            "🎱",
            "🚗",
            "✈️",
            "🚀",
            "⭐",
            "🌟",
            "✨",
            "⚡",
            "💯",
            "🎯",
        ],
    ),
]

_GRID_COLS = 8
_BUTTON_SIZE = 40


class EmojiPickerDialog(QDialog):
    """2026-09-11 v1.7.4:emoji 选择 grid — 取代 QInputDialog.getText。

    返回:`(emoji: str, is_big: bool)` 或 None(取消 / 关闭)。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("选择表情回应…"))
        self.setMinimumWidth(360)

        self._selected_emoji: str | None = None
        self._selected_is_big = False

        root = QVBoxLayout(self)

        # 类别 + emoji grid
        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)
        self._emoji_buttons: list[QPushButton] = []

        for label, emojis in _EMOJI_GROUPS:
            title_lbl = QLabel(self.tr(f"━━ {label} ━━"))
            title_lbl.setObjectName("emojiGroupTitle")
            root.addWidget(title_lbl)

            grid = QGridLayout()
            grid.setSpacing(4)
            for i, emoji in enumerate(emojis):
                btn = QPushButton(emoji)
                btn.setFixedSize(_BUTTON_SIZE, _BUTTON_SIZE)
                btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                btn.setCheckable(True)
                btn.setProperty("emojiChar", emoji)
                btn.clicked.connect(lambda _checked=False, e=emoji: self._on_emoji_clicked(e))
                self._button_group.addButton(btn)
                self._emoji_buttons.append(btn)
                grid.addWidget(btn, i // _GRID_COLS, i % _GRID_COLS)
            root.addLayout(grid)

        # 手输(兜底 — 非预置字符 / custom emoji id)
        manual_row = QHBoxLayout()
        manual_lbl = QLabel(self.tr("或手输(custom emoji id 用 `custom_emoji_id:N`):"))
        manual_lbl.setObjectName("emojiManualLabel")
        self._manual_input = QLineEdit()
        self._manual_input.setPlaceholderText(self.tr("例如 🔥 或 custom_emoji_id:123"))
        self._manual_input.setObjectName("emojiManualInput")
        self._manual_input.textChanged.connect(self._on_manual_text_changed)
        manual_row.addWidget(manual_lbl)
        manual_row.addWidget(self._manual_input, 1)
        root.addLayout(manual_row)

        # is_big toggle(custom emoji id 不支持,但用户手输时无法预判 — UI 始终允许,
        # 实际生效由 AppService.add_reaction 走 TDLib reactionTypeEmoji.is_big;
        # custom_emoji_id 路径 TDLib 端会忽略 is_big)。
        self._is_big_checkbox = QCheckBox(self.tr("大表情(animate;仅普通 emoji 生效)"))
        self._is_big_checkbox.setObjectName("emojiIsBigCheckbox")
        root.addWidget(self._is_big_checkbox)

        # OK / Cancel
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        root.addWidget(self._buttons)

    def _on_emoji_clicked(self, emoji: str) -> None:
        """点 grid 按钮 — 设选中 + 清手输(避免 grid / manual 双选中歧义)。"""
        self._selected_emoji = emoji
        self._manual_input.blockSignals(True)
        self._manual_input.clear()
        self._manual_input.blockSignals(False)
        self._update_ok_enabled()

    def _on_manual_text_changed(self, text: str) -> None:
        """手输变化 — 接管 selected(并取消 grid 高亮)。"""
        stripped = text.strip()
        self._selected_emoji = stripped if stripped else None
        if stripped:
            # 取消 grid 互斥高亮
            for btn in self._emoji_buttons:
                btn.setChecked(False)
        self._update_ok_enabled()

    def _update_ok_enabled(self) -> None:
        ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setEnabled(bool(self._selected_emoji))

    def accept(self) -> None:
        """覆写:校验 selected 非空 — 否则等同 reject。"""
        if not self._selected_emoji:
            return
        super().accept()

    def selected_emoji(self) -> str | None:
        """当前选中的 emoji(已 strip,非空) — 给上层 caller 看。"""
        return self._selected_emoji

    def is_big(self) -> bool:
        return self._is_big_checkbox.isChecked()

    @classmethod
    def get_emoji(cls, parent: QWidget | None = None) -> tuple[str, bool] | None:
        """2026-09-11 v1.7.4:静态入口 — 仿 `QInputDialog.getText`。

        Returns:
            `(emoji, is_big)` tuple,or None if cancelled / closed.
        """
        dlg = cls(parent)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        emoji = dlg.selected_emoji()
        if not emoji:
            return None
        return (emoji, dlg.is_big())
