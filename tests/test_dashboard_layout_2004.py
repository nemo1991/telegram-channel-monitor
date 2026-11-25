"""v2.0.4 FR-03 大盘整合契约 — 真实 MainWindow 守护布局改造。

- G3-1 实时流单实例：全窗口只有一个 MessageView / MessageDetail；
- G3-2 大盘为首页（stack page 0），导航 4 项；
- G3-3 实时流在统计卡下方，最近活动与频道统计左右并排；
- G3-4 MainWindow 仍持有 live_view / message_detail 引用（控制器依赖）；
- G3-5 页索引语义：媒体 idx=2。
"""

from __future__ import annotations

from PySide6.QtWidgets import QGroupBox, QVBoxLayout

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget


def _dashboard_layout(win) -> QVBoxLayout:
    assert isinstance(win.dashboard, DashboardWidget)
    layout = win.dashboard.layout()
    assert isinstance(layout, QVBoxLayout)
    return layout


def test_nav_has_three_items_and_dashboard_is_home(qapp, qloop, tmp_path):
    """G3-2 + v2.0.8：导航 4 项（新增消息管理页），大盘为首页。"""
    win = build_real_main_window(qloop, tmp_path)
    assert len(win.nav._buttons) == 4
    assert win.stack.count() == 4
    assert win.stack.widget(0) is win.dashboard
    assert win.stack.currentIndex() == 0
    win.close()


def _is_bottom_row(layout) -> bool:
    return any(
        isinstance(layout.itemAt(i).widget(), QGroupBox)
        and "最近活动" in layout.itemAt(i).widget().title()
        for i in range(layout.count())
    )
