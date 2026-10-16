"""MessageEmptyOverlay — 空状态三态 overlay + 新消息浮条 sub-widget。

2026-10-15 plan-batch7-B:从 `message_view.py` L558-585 / L809-902 整体搬迁,
主类降 908 → ~810 LOC。setter 通信模式(2026-10-15 用户决策):
  - view 不持 widget 内部状态,通过公开 setter / getter 读写
  - 浮条 click 走 Qt signal(`floating_clicked = Signal()`)上抛,
    view connect 后 widget 不持反向引用

公开契约:
  - `MessageEmptyOverlay(QWidget)` — 接受 parent:QWidget | None
  - `set_state(state: str)` — "no_subscribed" / "searching" / "live_empty"
  - `update_for_row_count(n: int)` — count==0 → show overlay
  - `set_messages_count(n: int)` / `messages_count() -> int` / `reset_counter()`
  - `show_floating_btn()` / `hide_floating_btn()`
  - `reposition(width: int, height: int)` — 居中 overlay + 浮条 top-right
  - `floating_clicked = Signal()` — 浮条 click 触发,view connect 后
    自行调 self.scrollToTop

通过 `from tgmonitor.ui.widgets.message_view import MessageEmptyOverlay`
re-export 保持旧 import 路径兼容。
"""

from __future__ import annotations

# 2026-10-15 plan-batch7-B:与 ui/ 文件惯例一致,屏蔽 PySide6 stub
# 对 QPalette / QCursor / Qt 枚举值的 attr-defined 误报。
# mypy: disable-error-code="attr-defined"
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

from tgmonitor.ui.widgets.form_row import empty_hint


class MessageEmptyOverlay(QWidget):
    """2026-09-14 v1.7.5 PR #5 (P0-L + P0-I):空状态三态 + 新消息浮条。

    2026-10-15 plan-batch7-B:从 MessageView 抽出独立 QWidget,走 setter 通信
    模式。view 不持 widget 内部状态(widget 单一 owner),通过公开 API
    读写 + 1 个 Qt signal(`floating_clicked`)通信。
    """

    # 2026-10-15 plan-batch7-B:浮条 click 触发,view connect 后调
    # self.scrollToTop。避免 sub-widget 反向调用 view。
    floating_clicked = Signal()

    # 2026-10-15 plan-batch7-B:三态文案集中常量 — 之前 set_empty_state 是
    # 一连串 if/elif,搬到 widget 后改成 dict 查表,主类 thin delegate
    # 不再关心具体文案。
    _EMPTY_STATES: dict[str, tuple[str, str, str]] = {
        "no_subscribed": (
            "💬",
            "未订阅频道",
            "先去「频道」页双击订阅一个频道,\n新消息会实时显示在这里。",
        ),
        "searching": (
            "🔍",
            "无匹配结果",
            "试试更换关键词或日期范围。",
        ),
        "live_empty": (
            "💬",
            "暂无消息",
            "新消息会实时显示。",
        ),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 1) empty_hint 三态 overlay — 默认显示,首条消息到达时由
        # update_for_row_count 切 hide。
        self._empty_overlay = empty_hint(
            icon="💬",
            title="暂无消息",
            hint="先去「频道」页双击订阅一个频道,\n新消息会实时显示在这里。",
            parent=self,
        )
        self._empty_overlay.raise_()
        # 2) 新消息浮条 — 默认隐藏,view 调 show_floating_btn() 才显示。
        self._floating_btn = QPushButton(self.tr("0 条新消息 ↓"), self)
        self._floating_btn.setObjectName("floatingNewMsgBtn")
        self._floating_btn.setCursor(Qt.PointingHandCursor)
        self._floating_btn.hide()
        # 浮条 click → emit floating_clicked → view connect 后调 scrollToTop
        self._floating_btn.clicked.connect(self.floating_clicked)
        # 3) 内部状态(原 MessageView 持有,搬到 widget 后 widget 单一 owner)。
        self._new_msg_count: int = 0
        self._empty_state: str = "no_subscribed"

    # ---- empty overlay API ----

    def set_state(self, state: str) -> None:
        """2026-09-14 v1.7.5 PR #5 (P0-L):切换空状态文案。

        Args:
            state: "no_subscribed" / "searching" / "live_empty"。
        """
        self._empty_state = state
        text = self._EMPTY_STATES.get(state)
        if text is None:
            return
        self._set_overlay_text(*text)

    def update_for_row_count(self, n: int) -> None:
        """count() == 0 → 显示 overlay,else 隐藏。

        signal 回调签名兼容 `rowsInserted(parent, first, last)` /
        `rowsRemoved(parent, first, last)` / `modelReset()`,所以 view
        端 connect 时 *args 透传,这里只接 count。
        """
        self._empty_overlay.setVisible(n == 0)
        self._empty_overlay.raise_()

    # ---- floating button API ----

    def set_messages_count(self, n: int) -> None:
        """改 label 文字(不自动 show — 由调用方决定 show_floating_btn)。"""
        self._new_msg_count = n
        self._floating_btn.setText(self.tr("%d 条新消息 ↓").format(n))

    def messages_count(self) -> int:
        return self._new_msg_count

    def reset_counter(self) -> None:
        """清零 + 隐藏(view 端 `clear_new_msg_counter` 透传)。"""
        self._new_msg_count = 0
        self._floating_btn.hide()

    def show_floating_btn(self) -> None:
        """显示 + 重定位(view rowsInserted 触发)。"""
        self._floating_btn.show()
        self._reposition_floating_btn()

    def hide_floating_btn(self) -> None:
        self._floating_btn.hide()

    # ---- layout / positioning ----

    def reposition(self, width: int, height: int) -> None:
        """窗口尺寸变 → overlay 重新居中(视觉重心偏上 1/3 高度)+
        浮条贴 top-right 留 16px padding。
        """
        # overlay 居中(上 1/3)
        hint_size = self._empty_overlay.sizeHint()
        x = max(0, (width - hint_size.width()) // 2)
        y = max(0, height // 3 - hint_size.height() // 2)
        self._empty_overlay.setGeometry(x, y, hint_size.width(), hint_size.height())
        self._empty_overlay.raise_()
        # 浮条 top-right(用 view 给的 width,view sizeHint 不准)
        if self._floating_btn.isVisible():
            size = self._floating_btn.sizeHint()
            fx = max(0, width - size.width() - 16)
            self._floating_btn.setGeometry(fx, 16, size.width(), size.height())
            self._floating_btn.raise_()

    def _reposition_floating_btn(self) -> None:
        """show_floating_btn / set_messages_count 内部调,按 widget 实际 width。"""
        if not self._floating_btn.isVisible():
            return
        size = self._floating_btn.sizeHint()
        x = max(0, self.width() - size.width() - 16)
        self._floating_btn.setGeometry(x, 16, size.width(), size.height())
        self._floating_btn.raise_()

    # ---- private helpers ----

    def _set_overlay_text(self, icon: str, title: str, hint: str) -> None:
        """直接改 overlay 内 icon / title / hint label 文字(不重建 widget)。

        empty_hint 返回 widget 含 3 个子 widget:icon QLabel + title QLabel +
        hint QLabel,顺序由 empty_hint 实现保证,通过 findChildren(QLabel)
        拿 label 后改 text。
        """
        labels = self._empty_overlay.findChildren(QLabel)
        # labels 顺序:[icon, title, hint](empty_hint 实现保证)
        if len(labels) >= 1:
            labels[0].setText(icon)
        if len(labels) >= 2:
            labels[1].setText(title)
        if len(labels) >= 3:
            labels[2].setText(hint)
        self._empty_overlay.raise_()
