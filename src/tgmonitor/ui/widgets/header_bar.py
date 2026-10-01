"""顶部紧凑 Header — 标题 + 搜索 + 状态徽章 + 操作按钮。

2026-10-01 v1.12.x 从 `MainWindow` 抽出(原 `_HeaderBar`,inline 在 `main_window.py:2118`):
不再用 QToolBar,改为自定义 widget,视觉更紧凑。子组件:
  - 左:`tgmonitor` 标题
  - 中:`SearchBar` 搜索框(共用 `widgets/search_bar.py`)
  - 右:状态徽章(state_dot + state_label) + 操作按钮(登录 / 验证码 / 2FA / 登出)+ 主题切换
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from tgmonitor.ui.state_labels import state_dot, state_label
from tgmonitor.ui.widgets.search_bar import SearchBar


class HeaderBar(QWidget):
    """顶部紧凑信息栏:左标题 + 搜索 + 右登录状态 + 操作。

    类变量无 None 占位 — `__init__` 内必建 `btn_logout/btn_action/btn_theme/search_bar`,
    mypy 看到实例属性 = QPushButton / SearchBar 而非 X | None,清掉 21 处 union-attr。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("headerBar")
        self.setFixedHeight(44)

        hbox = QHBoxLayout(self)
        hbox.setContentsMargins(16, 0, 16, 0)
        hbox.setSpacing(12)

        # 左: 标题
        title = QLabel(self.tr("tgmonitor"))
        title.setObjectName("appTitle")
        hbox.addWidget(title)

        # 搜索条
        self.search_bar = SearchBar()
        hbox.addWidget(self.search_bar)
        hbox.addStretch(1)

        # 右: 状态 + 操作
        self.state_dot = QLabel(self.tr("⚪"))
        self.state_dot.setFixedWidth(20)
        hbox.addWidget(self.state_dot)

        self.state_label = QLabel(self.tr("就绪"))
        self.state_label.setObjectName("headerState")
        hbox.addWidget(self.state_label)

        self.btn_action = QPushButton(self.tr("登录"))
        self.btn_action.setObjectName("headerActionBtn")
        self.btn_action.setVisible(False)
        hbox.addWidget(self.btn_action)

        self.btn_logout = QPushButton(self.tr("登出"))
        self.btn_logout.setObjectName("headerActionBtn")
        self.btn_logout.setVisible(False)
        hbox.addWidget(self.btn_logout)

        # 主题切换按钮 — 显示「当前切到该主题后会变成什么」
        from tgmonitor.ui.theme import ThemeManager

        cur = ThemeManager.current()
        self.btn_theme = QPushButton("🌙" if cur.value == "light" else "☀")
        self.btn_theme.setObjectName("headerActionBtn")
        self.btn_theme.setFixedWidth(36)
        # 2026-09-07 v1.6.8:tooltip 走 tr()(「切换主题」随 locale 翻译)。
        self.btn_theme.setToolTip(self.tr("切换主题(Ctrl+T)"))
        hbox.addWidget(self.btn_theme)

    def update_state(self, state: str, detail: str = "") -> None:
        dot = state_dot(state)
        label = state_label(state)
        if state == "error" and detail:
            label = f"{label}:{detail[:40]}"

        self.state_dot.setText(dot)
        self.state_label.setText(label)

        # 根据状态显隐操作按钮
        if state == "ready":
            self.btn_action.setVisible(False)
            self.btn_logout.setVisible(True)
        elif state in ("phone_required", "closed", "uninit"):
            self.btn_action.setText(self.tr("登录"))
            self.btn_action.setVisible(True)
            self.btn_logout.setVisible(False)
        elif state in ("code_required",):
            self.btn_action.setText(self.tr("验证码"))
            self.btn_action.setVisible(True)
            self.btn_logout.setVisible(False)
        elif state in ("password_required",):
            self.btn_action.setText(self.tr("2FA 密码"))
            self.btn_action.setVisible(True)
            self.btn_logout.setVisible(False)
        else:
            self.btn_action.setVisible(False)
            self.btn_logout.setVisible(False)
