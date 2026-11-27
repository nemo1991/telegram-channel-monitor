# mypy: disable-error-code="attr-defined"
"""VerticalNavBar — 左侧竖向导航(深色底,跨主题)。

4 个 Tab(v2.0.8 新增消息管理):
  0: 大盘(DASHBOARD)  — 统计 + 活动时间线 + 实时流
  1: 消息管理(MESSAGES) — 历史消息查看 / 频道·关键词·日期·收藏筛选
  2: 媒体管理(MEDIA)  — 浏览 / 重试 / 删 / 打开 + orphan reconcile
  3: 设置(SETTINGS)   — 所有配置(凭据/监听频道/存储/代理/媒体/同步)

2026-11-18 v2.0.6:撤销独立频道页 —— 监听频道管理迁入设置页,导航 4→3。
2026-11-19 v2.0.8:新增「消息」页,导航 3→4。

设计:
- 固定深色底 — 形成稳定的"导航锚点",不随系统设置变化
  **2.0.8 修复**:此前只在 _NavButton 上用 palette 设背景,父级
  VerticalNavBar 自身没有底色,idle 按钮又用 Qt.transparent → 整条 nav
  实际透出 QMainWindow 的 #f5f6fa 浅底,浅灰图标/文字与底色几乎同色,
  用户报"看不清 / 图标显示不全"。现在 nav 自身显式刷深色底 + QSS 双保险。
- 每个按钮 = 24px 图标 + 10px 标签文字(竖直堆叠)
  **2.0.8 修复**:此前按钮有 3px 透明左边框(占位防抖动),但文字标签是
  固定 64px 宽 → 在只剩 61px 的内容区里溢出,左侧被裁。去掉该边框,
  改由按钮整体尺寸保证不抖(active 有实色蓝底自证,不需要左边线)。
- 选中态:白色图标+文字 + accent 实色蓝底
- 悬停态:微亮背景(永远比 idle 亮、永远比 active 暗)
- **图标走 tinted_action_icon(显式注入 fg)**:Qt 的 QSvgRenderer 不解析
  currentColor,所以不能依赖 QSS color → SVG currentColor 链路 — 必须
  在 SVG 字节上直接替换为 hex。这是项目内 nav 唯一的图标加载入口。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.ui.icon import tinted_action_icon

# 标签走 `QCoreApplication.translate`,与其余 UI 文案写法一致。
# 2026-11-17 v2.0.4 FR-03:实时流迁入大盘,撤销独立页 → 4 项,大盘为首页。
# 2026-11-18 v2.0.6:频道页迁入设置页 → 3 项。
# 2026-11-19 v2.0.8:新增消息管理页 → 4 项。
_NAV_LABELS_SRC = ("大盘", "消息", "媒体", "设置")


def _nav_label(idx: int) -> str:
    """Return the translated label for nav item idx (0..3).

    用 module-level function 而不是直接调 translate,保持 `_NAV_ITEMS`
    结构(icon_name + label)的兼容性。
    """
    from PySide6.QtCore import QCoreApplication

    return QCoreApplication.translate("nav_bar", _NAV_LABELS_SRC[idx])


_NAV_ICONS = ("nav_dashboard", "nav_messages", "nav_media", "nav_settings")


def _nav_items() -> list[tuple[str, str]]:
    """导航项 (icon_name, label) 列表。"""
    return list(zip(_NAV_ICONS, (_nav_label(i) for i in range(len(_NAV_LABELS_SRC))), strict=True))


# ---- 配色 token(直接 hex,固定样式,本文件自包含) ----
#
# 2.0.9 二次修正:
#   - hover 与 active 收敛为**同一蓝色系**(hover=#4a7fd4 是 active=#5b9cf5
#     的暗一档),避免 2.0.8 的「深蓝灰 hover → 亮蓝 active」跳变突兀。
#   - 选中/悬浮态走 QSS 圆角胶囊(8px),去掉方方正正的直角块。
#
# 2.1.0 三次修正(idle 辨识度):
#   - **根因**:nav 底色此前靠 QSS/QPalette 设给 QWidget —— 这条路径跨平台
#     不稳(macOS Cocoa / Wayland 上 QSS background 常不生效),真机上整条
#     nav 透出主窗 #f5f6fa 浅底,而 idle 图标是纯白 → 与浅底接近、看不清。
#     2.1.0 改为 VerticalNavBar.paintEvent 用 QPainter.fillRect 直接画底色,
#     任何平台都生效,不再依赖 QSS/palette 背景解析。
#   - idle 前景从纯白改为**中亮暖灰 #b9c2d4**:即使个别平台仍有底色偏差,
#     中灰调也不会像纯白那样与浅底糊在一起;在深色底上与 nav_bg 亮度差
#     仍 >110,清晰可辨。
_LIGHT = {
    "nav_bg": "#1e2433",  # 导航条底色(深色锚点,与右侧 #f5f6fa 强区隔)
    "active_bg": "#5b9cf5",  # accent 实色 — 强对比
    "hover_bg": "#4a7fd4",  # 2.0.9:active 同色系暗一档,缩小 hover/active 色差
    "idle_bg": "transparent",
    "active_fg": "#ffffff",
    "inactive_fg": "#b9c2d4",  # 2.1.0:中亮暖灰,避免纯白与浅底糊在一起
    "accent": "#5b9cf5",
    "accent_soft": "rgba(91,156,245,0.22)",
    "pill_radius": "8",  # 2.0.9:选中/悬浮圆角半径(px)
}


def _palette() -> dict:
    return _LIGHT


class _NavButton(QWidget):
    """单个导航按钮:图标(24px)在上,标签在下,整体可点击。"""

    clicked = Signal(int)

    def __init__(self, index: int, icon_name: str, label: str) -> None:
        super().__init__()
        self._index = index
        self._active = False
        self._hovering = False
        self._icon_name = icon_name

        # 2.0.8:72×68 + 28px 图标容器(24px pixmap 居中留 2px padding),
        # 去掉旧 3px 透明左边框后文字标签 72px 正好铺满内容区,不再被裁切。
        self.setFixedSize(72, 68)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("navBtn")

        # 2.0.9:圆角背景层 —— 在按钮内缩 3px 放 QFrame,由 QSS 画圆角胶囊。
        # 不用按钮自身 QSS/palette 填背景的原因:
        #   - QSS `background` 在 QWidget 上跨平台不稳(macOS offscreen /
        #     Wayland 可能不生效),QFrame 对 border-radius 支持更可靠;
        #   - 内缩 3px + 8px 圆角 → 选中态是柔和圆角块,不再是贴边直角方块。
        self._bg = QFrame(self)
        self._bg.setObjectName("navBtnBg")
        self._bg.setGeometry(3, 3, 66, 62)
        self._bg.setStyleSheet("background: transparent; border: none;")
        self._bg.lower()  # 始终在图标/文字之下

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 8, 0, 6)
        vbox.setSpacing(4)
        vbox.setAlignment(Qt.AlignCenter)

        self._ico_label = QLabel()
        self._ico_label.setFixedSize(28, 28)
        self._ico_label.setAlignment(Qt.AlignCenter)
        vbox.addWidget(self._ico_label, 0, Qt.AlignCenter)

        self._txt_label = QLabel(label)
        self._txt_label.setAlignment(Qt.AlignCenter)
        self._txt_label.setFixedWidth(72)
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
        self._ico_label.setPixmap(icon.pixmap(24, 24))  # 28px 容器内居中,四周 2px padding 防裁切

        # ---- 背景:圆角胶囊层(active / hover / idle) ----
        # 2.0.9:hover 收敛到 active 同色系暗蓝,色差小、过渡自然;
        # 圆角 8px 消除「方方正正」的突兀感。idle 透明(nav 深色底自证)。
        if self._active:
            bg_hex = p["active_bg"]
        elif self._hovering:
            bg_hex = p["hover_bg"]
        else:
            bg_hex = "transparent"
        radius = p["pill_radius"]
        self._bg.setStyleSheet(
            f"QFrame#navBtnBg {{ background: {bg_hex}; border: none; border-radius: {radius}px; }}"
        )
        fg = QColor(p["active_fg"] if self._active else p["inactive_fg"])

        # 2.0.8:原先这里给所有态设 3px 透明左边框占位防抖,但文字标签
        # 固定 64px 宽 > 61px 内容区 → 图标/文字左侧被裁。现去掉边框,
        # 按钮固定尺寸本身保证不抖,active 用整块蓝底自证选中态。

        # ---- label palette + QSS(双保险) ----
        for lbl in (self._ico_label, self._txt_label):
            lbl.setAutoFillBackground(False)  # 不要 label 自己填底色
            lbl_pal = lbl.palette()
            lbl_pal.setColor(lbl.foregroundRole(), fg)
            lbl.setPalette(lbl_pal)
        self._ico_label.setStyleSheet("background: transparent;")
        self._txt_label.setStyleSheet(f"background: transparent;color:{fg.name()};font-size:10px;")

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
    """深色垂直导航栏,容纳 4 个 _NavButton。"""

    current_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        """建 4 个 _NavButton + 默认高亮第一个(0 = 大盘)。"""
        super().__init__(parent)
        self._current = 0
        self._buttons: list[_NavButton] = []

        # 2.0.8:nav 自身必须显式刷底色 —— 旧实现只给 _NavButton 用 palette,
        # idle 按钮是 Qt.transparent,于是整条 nav 透出主窗 #f5f6fa 浅底,
        # 浅灰前景几乎不可见。palette + QSS 双保险(跨平台)。
        p = _palette()
        self.setFixedWidth(76)
        self.setObjectName("navBar")
        nav_pal = self.palette()
        nav_pal.setColor(self.backgroundRole(), QColor(p["nav_bg"]))
        self.setAutoFillBackground(True)
        self.setPalette(nav_pal)
        self.setStyleSheet(f"QWidget#navBar {{ background: {p['nav_bg']}; }}")

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 16, 0, 0)
        vbox.setSpacing(4)
        vbox.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self._layout: QVBoxLayout = vbox  # add_item 插按钮用(避免 self.layout() 的 Optional)

        # 顶部 12px 留白 — 替代之前的 Unicode `●` logo(单字符跨字体
        # 渲染不一致,看着像占位)。v2.0.10 起页头已移除,留白保留作视觉呼吸。
        vbox.addSpacing(12)

        for idx, (icon_name, label) in enumerate(_nav_items()):
            btn = _NavButton(idx, icon_name, label)
            btn.clicked.connect(self._on_btn_clicked)
            self._buttons.append(btn)
            vbox.addWidget(btn, 0, Qt.AlignHCenter)

        vbox.addStretch(1)

        # 默认高亮第一个
        if self._buttons:
            self._buttons[0].set_active(True)

    def paintEvent(self, event) -> None:  # noqa: N802
        """自绘深色底 —— 绕开 QSS/QPalette background 在 QWidget 上跨平台不稳。

        项目历史坑(2.0.8 / 2.1.0):nav 底色靠 setStyleSheet/palette 设给
        QWidget,macOS Cocoa / Wayland 上 QSS background 常不生效 → 整条 nav
        透出主窗浅底,白色 idle 图标与浅底接近、看不清。QPainter.fillRect
        走的是 Qt 绘制底层,任何平台都生效,不依赖样式表解析。
        """
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(_palette()["nav_bg"]))
        painter.end()
        super().paintEvent(event)

    def _on_btn_clicked(self, idx: int) -> None:
        if idx == self._current:
            return
        self._buttons[self._current].set_active(False)
        self._buttons[idx].set_active(True)
        self._current = idx
        self.current_changed.emit(idx)

    def set_current(self, idx: int) -> None:
        """外部切 tab 的入口 — 越界静默忽略。"""
        if 0 <= idx < len(self._buttons):
            self._on_btn_clicked(idx)

    def add_item(self, icon_name: str, label: str) -> int:
        """追加一个导航项(插件 UI 扩展点)— 返回新按钮索引。

        插件项排在内置 4 项之后,索引由 `_buttons` 长度自然分配 ——
        `_on_btn_clicked` 按索引高亮 / 发 `current_changed`,无需改信号语义。
        图标名指向 `resources/icons/<name>.svg`,与内置项共用 `tinted_action_icon`
        着色链路(不存在的文件由 `tinted_action_icon` 走既有兜底)。

        调用方(`UiAssembler`)负责把同索引的页面加入 QStackedWidget,否则索引
        会错位 —— 两个容器必须同步增删。
        """
        idx = len(self._buttons)
        btn = _NavButton(idx, icon_name, label)
        btn.clicked.connect(self._on_btn_clicked)
        self._buttons.append(btn)
        # 插在 stretch 之前:布局尾部是 addStretch(1)(永远是最后一个 item),
        # 新按钮要排在它前面,否则会出现在导航条最底、与内置项间留一大段空白。
        self._layout.insertWidget(self._layout.count() - 1, btn, 0, Qt.AlignHCenter)
        return idx
