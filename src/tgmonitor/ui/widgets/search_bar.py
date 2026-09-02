# mypy: disable-error-code="attr-defined"
"""SearchBar — 全局搜索输入框,跨内容页过滤。

行为:
- 输入文本 → emit text_changed(str)
- 按 Enter → emit submitted(str)
- 右侧清除按钮
- 占位文字可定制
- 2026-09-02 v1.5.2 PR #B5:`📅 高级` 折叠按钮 → 展开 date_from / date_to
  `QDateTimeEdit` 行 + emit date_changed(datetime | None, datetime | None)。
  `clear()` 同时重置日期 + 折叠。

布局(原始):
  ┌────────────────────────────────────────────┐
  │ 🔍  [搜索消息、频道…]              ✕   📅  │  ← 32px,固定
  └────────────────────────────────────────────┘

布局(展开):
  ┌────────────────────────────────────────────┐
  │ 🔍  [搜索消息、频道…]              ✕   📅  │  ← 32px
  ├────────────────────────────────────────────┤
  │ 从 [2026-01-01 00:00:00 📅] 至 [2026-01-04 ✕ 不限] │  ← 36px
  └────────────────────────────────────────────┘
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDate, QDateTime, Qt, QTime, Signal
from PySide6.QtWidgets import (
    QDateTimeEdit,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class SearchBar(QWidget):
    """紧凑搜索框 — 280×32 宽,带 icon + 占位 + 清除按钮 + 高级日期折叠。

    2026-09-03 v1.5.3 PR #D2:加 🌐 scope toggle button + `scope_changed`
    signal — 控制搜索范围("已订阅" / "全部含已退订")。
    """

    text_changed = Signal(str)
    date_changed = Signal(object, object)  # (datetime | None, datetime | None)
    scope_changed = Signal(bool)  # True = all(全部) / False = subscribed(已订阅,默认)

    def __init__(
        self,
        placeholder: str = "搜索消息、频道…",
        parent: QWidget | None = None,
    ) -> None:
        """建 280px 宽紧凑搜索条 + 折叠 date panel(默认隐藏)+ 内联样式。"""
        super().__init__(parent)
        self.setObjectName("searchBar")
        self.setFixedWidth(280)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        # ---- 第一行(32px 固定):icon + QLineEdit + clear + 📅 折叠按钮 ----
        hbox = QHBoxLayout()
        hbox.setContentsMargins(8, 0, 8, 0)
        hbox.setSpacing(6)

        ico = QLabel("🔍")
        ico.setFixedWidth(16)
        ico.setAlignment(Qt.AlignCenter)
        hbox.addWidget(ico)

        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setFrame(False)
        self.edit.setFixedHeight(28)
        self.edit.textChanged.connect(self._on_text_changed)
        hbox.addWidget(self.edit, 1)

        self.btn_clear = QPushButton("✕")
        self.btn_clear.setFixedSize(20, 20)
        self.btn_clear.setFlat(True)
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.setVisible(False)
        self.btn_clear.clicked.connect(self.clear)
        hbox.addWidget(self.btn_clear)

        # 折叠按钮:📅 默认未选中,展开时第二行显示
        self.adv_btn = QToolButton()
        self.adv_btn.setText("📅")
        self.adv_btn.setCheckable(True)
        self.adv_btn.setFixedSize(24, 24)
        self.adv_btn.setCursor(Qt.PointingHandCursor)
        self.adv_btn.setToolTip("按日期范围过滤")
        hbox.addWidget(self.adv_btn)

        # 范围 toggle:🌐 默认未选(已订阅),选中后切「全部(含已退订频道历史)」
        # 2026-09-03 v1.5.3 PR #D2
        self.scope_btn = QToolButton()
        self.scope_btn.setText("🌐")
        self.scope_btn.setCheckable(True)
        self.scope_btn.setFixedSize(24, 24)
        self.scope_btn.setCursor(Qt.PointingHandCursor)
        self.scope_btn.setToolTip("搜索范围:已订阅(默认)/ 全部(含已退订频道历史)")
        hbox.addWidget(self.scope_btn)

        # 固定第一行 32px
        row1 = QWidget()
        row1.setFixedHeight(32)
        row1.setLayout(hbox)
        outer.addWidget(row1)

        # ---- 第二行(默认隐藏):date_from + date_to ----
        self._date_panel = QWidget()
        date_layout = QHBoxLayout(self._date_panel)
        date_layout.setContentsMargins(8, 0, 8, 0)
        date_layout.setSpacing(4)

        date_layout.addWidget(QLabel("从"))
        self.dt_from = QDateTimeEdit()
        self.dt_from.setCalendarPopup(True)
        self.dt_from.setDisplayFormat("yyyy-MM-dd HH:mm")
        # QDateTimeEdit 默认是当前时间 — 设个最小值 + 占位"不限",让用户看出
        # 何时"未设"。`_date_to_python()` 收到 ≤ 最小值时返 None(代表不限)。
        # 1900-01-01 00:00:00 — 用 QDate + QTime 构造(QDateTime 不支持
        # QDateTime(y, m, d) 三参签名,需 6 参或 QDate+QTime)。
        min_dt = QDateTime(QDate(1900, 1, 1), QTime(0, 0, 0))
        self.dt_from.setMinimumDateTime(min_dt)
        self.dt_from.setSpecialValueText("不限")
        self.dt_from.setDateTime(min_dt)  # 初始 = 不限
        date_layout.addWidget(self.dt_from)

        date_layout.addWidget(QLabel("至"))
        self.dt_to = QDateTimeEdit()
        self.dt_to.setCalendarPopup(True)
        self.dt_to.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dt_to.setMinimumDateTime(min_dt)
        self.dt_to.setSpecialValueText("不限")
        self.dt_to.setDateTime(min_dt)  # 初始 = 不限
        date_layout.addWidget(self.dt_to)

        self._date_panel.setVisible(False)
        outer.addWidget(self._date_panel)

        self.adv_btn.toggled.connect(self._date_panel.setVisible)
        self.dt_from.dateTimeChanged.connect(self._emit_date)
        self.dt_to.dateTimeChanged.connect(self._emit_date)
        self.scope_btn.toggled.connect(self._on_scope_changed)

        self.setStyleSheet(
            "SearchBar, #searchBar {"
            "  background: #f0f1f5;"
            "  border: 1px solid #e2e4e9;"
            "  border-radius: 16px;"
            "}"
            "QLineEdit { background: transparent; border: none; padding: 4px 0; }"
            "QPushButton, QToolButton { background: transparent; border: none; color: #8a8d92; font-size: 13px; }"
            "QPushButton:hover, QToolButton:hover { color: #1a1a2e; }"
        )

    def text(self) -> str:
        """当前输入文本(strip 首尾空白)— 给 caller 直接读用。"""
        return self.edit.text().strip()

    def date_range(self) -> tuple[datetime | None, datetime | None]:
        """当前 date_from / date_to(不限 → None)。"""
        return self._date_to_python(self.dt_from), self._date_to_python(self.dt_to)

    def scope(self) -> bool:
        """搜索范围:`True` = 全部(含已退订频道历史)/ `False` = 已订阅(默认)。

        2026-09-03 v1.5.3 PR #D2:`scope_btn` toggle 状态。
        """
        return self.scope_btn.isChecked()

    @staticmethod
    def _date_to_python(editor: QDateTimeEdit) -> datetime | None:
        """QDateTimeEdit → Python datetime。"不限"特殊值(= 1900-01-01)→ None。"""
        qt = editor.dateTime()
        # specialValueText 触发条件是当前值 == minimumDateTime,等价于
        # `editor.dateTime() == editor.minimumDateTime()`。
        if qt == editor.minimumDateTime():
            return None
        # `QDateTime.toPython()` 静态返回类型是 object(PySide6 签名),
        # 实际是 datetime(QTime=0,0,0 时)或 QDate(无 time)。displayFormat 含
        # HH:mm → 必有 time → to_python 实际就是 datetime。cast 走通 mypy strict。
        from typing import cast

        return cast(datetime, qt.toPython())

    def clear(self) -> None:
        """清空输入 + 重置日期 + 隐藏 date panel + 折叠按钮 + 重置 scope。"""
        self.edit.clear()
        self.btn_clear.setVisible(False)
        # 重置日期为 "不限" 状态
        self.dt_from.setDateTime(self.dt_from.minimumDateTime())
        self.dt_to.setDateTime(self.dt_to.minimumDateTime())
        self._date_panel.setVisible(False)
        self.adv_btn.setChecked(False)
        # 2026-09-03 v1.5.3 PR #D2:重置 scope 到「已订阅」(默认)
        self.scope_btn.setChecked(False)
        # 不显式 emit text_changed / date_changed / scope_changed — Qt 自己
        # 触发(QDateTimeEdit reset → dateTimeChanged signal;QLineEdit
        # clear → textChanged signal;QToolButton.setChecked → toggled signal)。

    def _on_text_changed(self, txt: str) -> None:
        self.btn_clear.setVisible(bool(txt))
        self.text_changed.emit(txt.strip())

    def _emit_date(self) -> None:
        """date_from / date_to 任一变化 → emit date_changed。"""
        f, t = self.date_range()
        self.date_changed.emit(f, t)

    def _on_scope_changed(self, checked: bool) -> None:
        """2026-09-03 v1.5.3 PR #D2:scope toggle → emit scope_changed。

        `True` = 全部(含已退订频道历史)/ `False` = 已订阅(默认)。
        MainWindow 用此触发 `_search_debounce` 重拉(不 clear view)。
        """
        self.scope_changed.emit(checked)
