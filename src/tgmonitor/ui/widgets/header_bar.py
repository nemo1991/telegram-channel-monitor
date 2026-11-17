"""顶部紧凑 Header — 2026-11-17 v2.0.3 D3 起只剩纯标题。

历史:
- 2026-10-01 v1.12.x 从 `MainWindow` 抽出(原 `_HeaderBar`),标题 + 状态徽章
  + 登录/登出按钮,固定高 44px。
- 2026-11-16 v2.0.2:移除中部 `SearchBar`。
- 2026-11-17 v2.0.3:登录态合并进底部单一状态位(`StatusBar._UnifiedStateLabel`),
  登录/登出入口迁移到 `SettingsPage`「📱 账户凭证」分组。头栏仅保留
  `tgmonitor` 标题作品牌锚点(`nav_bar.py` 注释依赖该锚点),不再暴露
  `state_dot` / `state_label` / `btn_action` / `btn_logout` / `update_state`。
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget


class HeaderBar(QWidget):
    """纯标题头栏 — 状态与操作已分别下沉到底部状态栏与设置页。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("headerBar")
        self.setFixedHeight(44)

        hbox = QHBoxLayout(self)
        hbox.setContentsMargins(16, 0, 16, 0)
        hbox.setSpacing(12)

        title = QLabel(self.tr("tgmonitor"))
        title.setObjectName("appTitle")
        hbox.addWidget(title)
        hbox.addStretch(1)
