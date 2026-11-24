"""v2.0.1 FIX-01 回归:状态栏必须在内容区底部,而不是浮在窗口左上角。

根因(2026-11-16 真实窗口探针坐实):`UiAssembler.build_central()` 创建
`StatusBar(..., parent=self._mw)` 后**没有** `right_layout.addWidget(...)`。
parent 只决定对象树,不决定布局 —— 状态栏成了 MainWindow 的直接子 widget,
geometry 停在 `(0, 0, 100, 30)`,Z-order 又高于 central widget,于是浮在
顶部。`right_layout` 里只有 QStackedWidget + statusBar 两项(v2.0.10 起无头栏)。
"""

from __future__ import annotations

from pathlib import Path

from tests.fixtures._real_window import build_real_main_window


def test_status_bar_is_last_item_of_right_layout(qapp, qloop, tmp_path):
    """状态栏归 right_layout 管,且排在 stack 之后(物理位于底部)。"""
    win = build_real_main_window(qloop, tmp_path)

    central_layout = win.centralWidget().layout()
    assert central_layout is not None
    right = central_layout.itemAt(1).widget()
    assert right is not None and right.objectName() == "contentArea"

    assert win.status_bar.parent() is right, (
        "StatusBar 必须是内容区的子 widget,而不是 MainWindow 的直接子 widget"
    )

    right_layout = right.layout()
    assert right_layout is not None
    stack_idx = right_layout.indexOf(win.stack)
    assert stack_idx >= 0
    assert right_layout.indexOf(win.status_bar) == stack_idx + 1, (
        "StatusBar 必须紧跟 QStackedWidget,由布局把它压在内容区底部"
    )


def test_status_bar_sits_below_content_and_stays_at_bottom(qapp, qloop, tmp_path):
    """show / resize 后状态栏都在内容下方且贴底,不再停在 (0, 0)。"""
    win = build_real_main_window(qloop, tmp_path)

    stack = win.stack.geometry()
    bar = win.status_bar.geometry()
    assert bar.y() >= stack.y() + stack.height() - 1, (
        f"状态栏 y={bar.y()} 应位于内容区底部(stack bottom={stack.y() + stack.height()})"
    )

    win.resize(900, 600)
    qapp.processEvents()
    right = win.status_bar.parent()
    bar2 = win.status_bar.geometry()
    assert bar2.y() > 0, "缩窗后状态栏不能回到窗口顶部"
    assert bar2.y() + bar2.height() <= right.height() + 1, "缩窗后状态栏仍应贴内容区底边"


def test_header_row_removed_frees_content_space(qapp, qloop, tmp_path):
    """v2.0.10:页头整块移除后 stack 顶到内容区 y=0,原 44px 让给内容。"""
    from PySide6.QtWidgets import QWidget

    win = build_real_main_window(qloop, tmp_path)
    assert not hasattr(win, "header")
    assert win.findChild(QWidget, "headerBar") is None
    assert win.findChild(QWidget, "appTitle") is None

    right = win.status_bar.parent()
    assert right is not None
    assert right.layout().indexOf(win.stack) == 0, "stack 应是内容区首项,无头栏占位"

    qapp.processEvents()
    assert win.stack.geometry().top() == 0, "stack 顶边应为 0(原页头 44px 已释放)"
    win.close()


def test_status_bar_has_tinted_background() -> None:
    """状态栏用内容区浅灰底 #f5f6fa,与白色卡片在视觉上分层。"""
    qss = (Path(__file__).resolve().parents[1] / "src/tgmonitor/ui/resources/style.qss").read_text(
        encoding="utf-8"
    )
    block = qss.split("#customStatusBar {", 1)[1].split("}", 1)[0]
    assert "background: #f5f6fa;" in block
