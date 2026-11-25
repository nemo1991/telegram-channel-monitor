"""v2.0.1 FIX-01 回归:状态栏必须在内容区底部,而不是浮在窗口左上角。

根因(2026-11-16 真实窗口探针坐实):`UiAssembler.build_central()` 创建
`StatusBar(..., parent=self._mw)` 后**没有** `right_layout.addWidget(...)`。
parent 只决定对象树,不决定布局 —— 状态栏成了 MainWindow 的直接子 widget,
geometry 停在 `(0, 0, 100, 30)`,Z-order 又高于 central widget,于是浮在
顶部。`right_layout` 里只有 QStackedWidget + statusBar 两项(v2.0.10 起无头栏)。
"""

from __future__ import annotations

from tests.fixtures._real_window import build_real_main_window


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
