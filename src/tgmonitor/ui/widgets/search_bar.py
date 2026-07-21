"""SearchBar — 全局搜索输入框,跨内容页过滤。

行为:
- 输入文本 → emit text_changed(str)
- 按 Enter → emit submitted(str)
- 右侧清除按钮
- 占位文字可定制
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)


class SearchBar(QWidget):
    """紧凑搜索框 — 240px 宽,带 icon + 占位 + 清除按钮。"""

    text_changed = Signal(str)

    def __init__(
        self,
        placeholder: str = "搜索消息、频道…",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("searchBar")
        self.setFixedWidth(280)
        self.setFixedHeight(32)

        hbox = QHBoxLayout(self)
        hbox.setContentsMargins(8, 0, 8, 0)
        hbox.setSpacing(6)

        # 放大镜 emoji (避免再加 SVG)
        ico = QLabel("🔍")
        ico.setFixedWidth(16)
        ico.setAlignment(Qt.AlignCenter)
        hbox.addWidget(ico)

        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setFrame(False)
        self.edit.textChanged.connect(self._on_text_changed)
        hbox.addWidget(self.edit, 1)

        self.btn_clear = QPushButton("✕")
        self.btn_clear.setFixedSize(20, 20)
        self.btn_clear.setFlat(True)
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.setVisible(False)
        self.btn_clear.clicked.connect(self.clear)
        hbox.addWidget(self.btn_clear)

        self.setStyleSheet(
            "SearchBar, #searchBar {"
            "  background: #f0f1f5;"
            "  border: 1px solid #e2e4e9;"
            "  border-radius: 16px;"
            "}"
            "QLineEdit { background: transparent; border: none; padding: 4px 0; }"
            "QPushButton { background: transparent; border: none; color: #8a8d92; font-size: 11px; }"
            "QPushButton:hover { color: #1a1a2e; }"
        )

    def text(self) -> str:
        return self.edit.text().strip()

    def clear(self) -> None:
        self.edit.clear()
        self.btn_clear.setVisible(False)

    def _on_text_changed(self, txt: str) -> None:
        self.btn_clear.setVisible(bool(txt))
        self.text_changed.emit(txt.strip())