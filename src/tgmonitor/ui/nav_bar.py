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
#
# 关键设计:**active 用纯 accent 实色填充**,跟父级 nav 底(#1e1e2e)有
# 强烈对比。原来 active 也用深色,跟父级同色,用户根本看不出选中。
# 3 态(active / hover / idle)必须有清晰对比:
#   active  >  hover  >  idle
_DARK = {
    # 父级 nav bg = #1a1a26(由 QSS `#navBar` 设)
    "active_bg": "#5b9cf5",       # accent 实色 — 强烈对比,选中立刻可见
    "hover_bg": "#2c2c45",        # 比 idle 亮 1 阶
    "idle_bg": "transparent",
    "active_fg": "#ffffff",       # 白字在蓝实色上,WCAG 5.3:1
    "inactive_fg": "#b0b5c8",     # 浅灰(WCAG AA 对 #1a1a26)
    "accent": "#5b9cf5",
    "accent_soft": "rgba(91,156,245,0.18)",  # hover 用(在 dark active 上太亮反而刺眼)
}
_LIGHT = {
    # 父级 nav bg = #1a1a26(浅色主题 nav 底故意保持深色做"锚点")
    "active_bg": "#5b9cf5",       # accent 实色 — 强对比
    "hover_bg": "#2c2c45",        # 比父级 #1a1a26 亮一阶
    "idle_bg": "transparent",
    "active_fg": "#ffffff",
    "inactive_fg": "#b0b5c8",
    "accent": "#5b9cf5",
    "accent_soft": "rgba(91,156,245,0.22)",
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
        # 用当前主题的明确 fg 注入 SVG(避开 QSvgRenderer 不解析 currentColor 的坑)。
        if self._active:
            icon = tinted_action_icon(self._icon_name, QColor(p["active_fg"]))
        else:
            icon = tinted_action_icon(self._icon_name, QColor(p["inactive_fg"]))
        self._ico_label.setPixmap(icon.pixmap(24, 24))

        # ---- 背景:active / hover / idle 三态清晰分层 ----
        # **重要**:除 QSS 外,再用 setAutoFillBackground + QPalette 兜底。
        # 在某些 Qt 平台(macOS offscreen、Wayland)QSS 的 `background` 在
        # QWidget 上可能不生效;走 palette 是 Qt 最稳的路径,跟平台无关。
        if self._active:
            bg = QColor(p["active_bg"])
        elif self._hovering:
            bg = QColor(p["hover_bg"])
        else:
            bg = Qt.transparent
        fg = QColor(p["active_fg"] if self._active else p["inactive_fg"])

        # ---- Palette:背景色(最稳) ----
        pal = self.palette()
        pal.setColor(self.backgroundRole(), bg)
        self.setAutoFillBackground(True)
        self.setPalette(pal)

        # ---- QSS:左侧 3px 占位防 layout 抖动,active 时无 accent(蓝底自证) ----
        # active 用实色蓝底,无需额外左边线;非 active 给一个透明 3px 占位
        # 让按钮宽度保持 64 不变(不切换 active 时整条 nav 不抖)。
        border_left = "3px solid transparent"
        self.setStyleSheet(
            f"border-left:{border_left};"
            f"border-top:0;border-right:0;border-bottom:0;"
        )

        # ---- label palette + QSS(双保险) ----
        for lbl in (self._ico_label, self._txt_label):
            lbl.setAutoFillBackground(False)  # 不要 label 自己填底色
            lbl_pal = lbl.palette()
            lbl_pal.setColor(lbl.foregroundRole(), fg)
            lbl.setPalette(lbl_pal)
        self._ico_label.setStyleSheet("background: transparent;")
        self._txt_label.setStyleSheet(
            f"background: transparent;color:{fg.name()};font-size:10px;"
        )

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
