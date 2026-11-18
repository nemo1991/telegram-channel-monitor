"""MessageEmptyOverlay — 空状态三态 overlay sub-widget。

2026-10-15 plan-batch7-B:从 `message_view.py` L558-585 / L809-902 整体搬迁,
主类降 908 → ~810 LOC。setter 通信模式(2026-10-15 用户决策):
  - view 不持 widget 内部状态,通过公开 setter / getter 读写
  - 三态文案集中常量,主类 thin delegate 不关心具体文案

公开契约:
  - `MessageEmptyOverlay(QWidget)` — 接受 parent:QWidget | None
  - `set_state(state: str)` — "no_monitored" / "searching" / "live_empty"
  - `update_for_row_count(n: int)` — count==0 → show overlay
  - `reposition(width: int, height: int)` — 居中 overlay

2026-11-18 v2.0.5:移除「N 条新消息 ↓」浮条 — 用户确认无实际作用,与
LIVE 只读看板定位冲突。原有的 floating_clicked signal / set_messages_count /
show_floating_btn / reset_counter 等浮条 API 一并删除,空状态三态不受影响。

通过 `from tgmonitor.ui.widgets.message_view import MessageEmptyOverlay`
re-export 保持旧 import 路径兼容。
"""

from __future__ import annotations

# 2026-10-15 plan-batch7-B:与 ui/ 文件惯例一致,屏蔽 PySide6 stub
# 对 QPalette / QCursor / Qt 枚举值的 attr-defined 误报。
# mypy: disable-error-code="attr-defined"
from PySide6.QtWidgets import QLabel, QWidget

from tgmonitor.ui.widgets.form_row import empty_hint


class MessageEmptyOverlay(QWidget):
    """2026-09-14 v1.7.5 PR #5 (P0-L):空状态三态 overlay。

    2026-10-15 plan-batch7-B:从 MessageView 抽出独立 QWidget,走 setter 通信
    模式。view 不持 widget 内部状态(widget 单一 owner),通过公开 API 读写。
    2026-11-18:新消息浮条已移除,本 widget 只负责空状态三态。
    """

    # 2026-10-15 plan-batch7-B:三态文案集中常量 — 之前 set_empty_state 是
    # 一连串 if/elif,搬到 widget 后改成 dict 查表,主类 thin delegate
    # 不再关心具体文案。
    _EMPTY_STATES: dict[str, tuple[str, str, str]] = {
        "no_monitored": (
            "消息",
            "未订阅频道",
            "先去「设置 → 监听频道管理」加入监听,\n新消息会实时显示在这里。",
        ),
        "searching": (
            "搜索",
            "无匹配结果",
            "试试更换关键词或日期范围。",
        ),
        "live_empty": (
            "消息",
            "暂无消息",
            "新消息会实时显示。",
        ),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # empty_hint 三态 overlay — 默认显示,首条消息到达时由
        # update_for_row_count 切 hide。
        self._empty_overlay = empty_hint(
            icon="消息",
            title="暂无消息",
            hint="先去「设置 → 监听频道管理」加入监听,\n新消息会实时显示在这里。",
            parent=self,
        )
        self._empty_overlay.raise_()
        self._empty_state: str = "no_monitored"

    # ---- empty overlay API ----

    def set_state(self, state: str) -> None:
        """2026-09-14 v1.7.5 PR #5 (P0-L):切换空状态文案。

        Args:
            state: "no_monitored" / "searching" / "live_empty"。
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

    # ---- layout / positioning ----

    def reposition(self, width: int, height: int) -> None:
        """窗口尺寸变 → overlay 重新居中(视觉重心偏上 1/3 高度)。"""
        hint_size = self._empty_overlay.sizeHint()
        x = max(0, (width - hint_size.width()) // 2)
        y = max(0, height // 3 - hint_size.height() // 2)
        self._empty_overlay.setGeometry(x, y, hint_size.width(), hint_size.height())
        self._empty_overlay.raise_()

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
