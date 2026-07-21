"""VerticalNavBar — 左侧竖向导航(深色底,跨主题)。

4 个 Tab:
  0: 实时流(LIVE)   — 实时消息流
  1: 大盘(DASHBOARD) — 统计 + 活动时间线
  2: 频道(CHANNELS) — 频道管理(订阅/退订/同步)
  3: 设置(SETTINGS) — 所有配置(凭据/存储/代理/媒体/同步)

设计:
- 在浅色和暗色模式下都用深色底 — 形成稳定的"导航锚点",不被主题切换影响
- 每个按钮 = 24px 图标 + 10px 标签文字(竖直堆叠)
- 选中态:白色图标+文字 + 左侧 3px accent 条 + 浅蓝渐变叠层
- 悬停态:微亮背景(永远比 idle 亮、永远比 active 暗)
- **图标走 tinted_action_icon(显式注入 fg)**:Qt 的 QSvgRenderer 不解析
  currentColor,所以不能依赖 QSS color → SVG currentColor 链路 — 必须
  在 SVG 字节上直接替换为 hex。这是项目内 nav 唯一的图标加载入口。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QLabel,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.ui.icon import tinted_action_icon
from tgmonitor.ui.theme import Theme, ThemeManager

_NAV_ITEMS = [
    ("nav_live", "实时流"),
    ("nav_dashboard", "大盘"),
    ("nav_channels", "频道"),
    ("nav_settings", "设置"),
]

# ---- 配色 token(直接 hex,不走 ThemeManager accent(),让本文件自包含) ----
# 主题切换时按 DARK/LIGHT 二选一,新主题再加分支即可。
_DARK = {
    "active_bg": "#3a3a55",       # 比 hover 亮 1 阶
    "hover_bg": "#2c2c45",        # 比 idle bg(#1e1e2e)亮 1 阶
    "idle_bg": "transparent",
    "active_fg": "#ffffff",
    "inactive_fg": "#b0b5c8",     # WCAG AA 过(对 #1e1e2e)
    "accent": "#7bb4ff",          # 亮色主题感更强
    "accent_overlay": "rgba(123,180,255,0.18)",
    "accent_glow": "rgba(123,180,255,0.35)",
}
_LIGHT = {
    "active_bg": "#1e1e2e",       # light 模式 nav 底故意保持深色(锚点)
    "hover_bg": "#2a2a40",        # 比 active 暗 1 阶 — 修正"hover 反向"
    "idle_bg": "transparent",
    "active_fg": "#ffffff",
    "inactive_fg": "#b0b5c8",     # light 主题下也用浅灰(fg 对深底通用)
    "accent": "#5b9cf5",          # 浅色主题标准蓝
    "accent_overlay": "rgba(91,156,245,0.22)",
    "accent_glow": "rgba(91,156,245,0.40)",
}


def _palette() -> dict:
    return _DARK if ThemeManager.current() == Theme.DARK else _LIGHT


class _NavButton(QWidget):
    """单个导航按钮:图标(24px)在上,标签在下,整体可点击。"""

    clicked = Signal(int)

    def __init__(self, index: int, icon_name: str, label: str) -> None:
        super().__init__()
        self._index = index
        self._active = False
        self._hovering = False
        self._icon_name = icon_name

        self.setFixedSize(64, 64)
        self.setCursor(Qt.PointingHandCursor)

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 6, 0, 4)
        vbox.setSpacing(2)
        vbox.setAlignment(Qt.AlignCenter)

        self._ico_label = QLabel()
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
        p = _palette()
        # ---- 图标:按状态用 tinted 出来的两套 ----
        if self._active:
            icon = tinted_action_icon(self._icon_name, QColor(p["active_fg"]))
        else:
            icon = tinted_action_icon(self._icon_name, QColor(p["inactive_fg"]))
        self._ico_label.setPixmap(icon.pixmap(24, 24))

        # ---- 背景:active / hover / idle 三态清晰分层 ----
        if self._active:
            bg = p["active_bg"]
        elif self._hovering:
            bg = p["hover_bg"]
        else:
            bg = p["idle_bg"]
        fg = p["active_fg"] if self._active else p["inactive_fg"]

        # active 时:左 accent 条 + 浅蓝渐变叠层 + 微 glow 描边
        # 否则:左 3px 透明占位(防止 1px 抖动)
        if self._active:
            border_left = f"3px solid {p['accent']}"
            extra = (
                f"background-image: linear-gradient(90deg, {p['accent_overlay']}, transparent);"
                f"border-top: 1px solid {p['accent_glow']};"
                f"border-right: 1px solid {p['accent_glow']};"
                f"border-bottom: 1px solid {p['accent_glow']};"
            )
        else:
            border_left = "3px solid transparent"
            extra = ""

        self.setStyleSheet(
            f"background:{bg};border-left:{border_left};"
            f"border-top:none;border-right:none;border-bottom:none;{extra}"
        )
        self._txt_label.setStyleSheet(f"color:{fg};font-size:10px;")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        super().mousePressEvent(event)
        self.clicked.emit(self._index)

    def enterEvent(self, event) -> None:  # noqa: N802
        if not self._active:
            self._hovering = True
            self._refresh_style()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hovering:
            self._hovering = False
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
        vbox.setContentsMargins(0, 16, 0, 0)
        vbox.setSpacing(4)
        vbox.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        # 顶部 12px 留白 — 替代之前的 Unicode `●` logo(单字符跨字体
        # # 渲染不一致,看着像占位)。header bar 已有 `appTitle` 文本品牌锚点,
        # # nav 不再重复。
        vbox.addSpacing(12)

        for idx, (icon_name, label) in enumerate(_NAV_ITEMS):
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

    def refresh_theme(self) -> None:
        """主题切换后调用,刷新所有按钮颜色 + 图标。"""
        for btn in self._buttons:
            btn._refresh_style()
