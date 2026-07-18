"""VerticalNavBar — 左侧竖向导航(暗色底,图标+标签)。

4 个 Tab:
  0: 实时流(LIVE)   — 实时消息流
  1: 大盘(DASHBOARD) — 统计 + 活动时间线
  2: 频道(CHANNELS) — 频道管理(订阅/退订/同步)
  3: 设置(SETTINGS) — 所有配置(凭据/存储/代理/媒体/同步)

设计:
- 暗色底(#1e1e2e → #16162a),与内容区白底形成强烈对比
- 每个按钮 = 24px 图标 + 10px 标签文字(竖直堆叠)
- 选中态:白色图标+文字 + 左侧 3px accent 条
- 悬停态:微亮背景
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.ui.icon import action_icon

_NAV_ITEMS = [
    ("nav_live", "实时流"),
    ("nav_dashboard", "大盘"),
    ("nav_channels", "频道"),
    ("nav_settings", "设置"),
]


class _NavButton(QWidget):
    """单个导航按钮:图标(24px)在上,标签在下,整体可点击。"""

    clicked = Signal(int)

    def __init__(self, index: int, icon_name: str, label: str) -> None:
        super().__init__()
        self._index = index
        self._active = False

        self.setFixedSize(64, 64)
        self.setCursor(Qt.PointingHandCursor)

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 6, 0, 4)
        vbox.setSpacing(2)
        vbox.setAlignment(Qt.AlignCenter)

        self._ico_label = QLabel()
        pix = action_icon(icon_name).pixmap(24, 24)
        self._ico_label.setPixmap(pix)
        self._ico_label.setFixedSize(24, 24)
        self._ico_label.setAlignment(Qt.AlignCenter)
        vbox.addWidget(self._ico_label, 0, Qt.AlignCenter)

        self._txt_label = QLabel(label)
        self._txt_label.setAlignment(Qt.AlignCenter)
        self._txt_label.setFixedWidth(64)
        vbox.addWidget(self._txt_label, 0, Qt.AlignCenter)

        self._refresh_style()

    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self._refresh_style()

    def _refresh_style(self) -> None:
        if self._active:
            bg = "#2a2a3e"
            fg = "#ffffff"
            border_left = "3px solid #5b9cf5"
        else:
            bg = "transparent"
            fg = "#8a8fa8"
            border_left = "3px solid transparent"
        self.setStyleSheet(
            f"background:{bg};border-left:{border_left};"
            f"border-top:none;border-right:none;border-bottom:none;"
        )
        self._txt_label.setStyleSheet(f"color:{fg};font-size:10px;")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        super().mousePressEvent(event)
        self.clicked.emit(self._index)

    def enterEvent(self, event) -> None:  # noqa: N802
        if not self._active:
            self.setStyleSheet(
                "background:#252540;border-left:3px solid transparent;"
                "border-top:none;border-right:none;border-bottom:none;"
            )
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if not self._active:
            self._refresh_style()
        super().leaveEvent(event)


class VerticalNavBar(QWidget):
    """暗色垂直导航栏,容纳 4 个 _NavButton。"""

    current_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current = 0
        self._buttons: list[_NavButton] = []

        self.setFixedWidth(68)
        self.setObjectName("navBar")

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 20, 0, 0)
        vbox.setSpacing(4)
        vbox.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        # app 标志(小圆点 logo)
        logo = QLabel("●")
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedWidth(64)
        logo.setStyleSheet("color:#5b9cf5;font-size:20px;font-weight:bold;")
        vbox.addWidget(logo)
        vbox.addSpacing(8)

        for idx, icon_name, label in zip(
            range(len(_NAV_ITEMS)),
            [it[0] for it in _NAV_ITEMS],
            [it[1] for it in _NAV_ITEMS],
            strict=False,
        ):
            btn = _NavButton(idx, icon_name, label)
            btn.clicked.connect(self._on_btn_clicked)
            self._buttons.append(btn)
            vbox.addWidget(btn, 0, Qt.AlignHCenter)

        vbox.addStretch(1)

        # 默认高亮第一个
        if self._buttons:
            self._buttons[0].set_active(True)

    def _on_btn_clicked(self, idx: int) -> None:
        if idx == self._current:
            return
        self._buttons[self._current].set_active(False)
        self._buttons[idx].set_active(True)
        self._current = idx
        self.current_changed.emit(idx)

    def set_current(self, idx: int) -> None:
        if 0 <= idx < len(self._buttons):
            self._on_btn_clicked(idx)
