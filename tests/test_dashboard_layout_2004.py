"""v2.0.4 FR-03 大盘整合契约 — 真实 MainWindow 守护布局改造。

- G3-1 实时流单实例：全窗口只有一个 MessageView / MessageDetail；
- G3-2 大盘为首页（stack page 0），导航 4 项；
- G3-3 实时流在统计卡下方，最近活动与频道统计左右并排；
- G3-4 MainWindow 仍持有 live_view / message_detail 引用（控制器依赖）；
- G3-5 页索引语义：媒体 idx=2。
"""

from __future__ import annotations

from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QSplitter, QVBoxLayout

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget
from tgmonitor.ui.widgets.message_detail import MessageDetail
from tgmonitor.ui.widgets.message_view import MessageView


def _dashboard_layout(win) -> QVBoxLayout:
    assert isinstance(win.dashboard, DashboardWidget)
    layout = win.dashboard.layout()
    assert isinstance(layout, QVBoxLayout)
    return layout


def test_nav_has_four_items_and_dashboard_is_home(qapp, qloop, tmp_path):
    """G3-2：导航 4 项，大盘为首页。"""
    win = build_real_main_window(qloop, tmp_path)
    assert len(win.nav._buttons) == 4
    assert win.stack.count() == 4
    assert win.stack.widget(0) is win.dashboard
    assert win.stack.currentIndex() == 0
    win.close()


def test_live_stream_is_single_instance(qapp, qloop, tmp_path):
    """G3-1 + G3-4：全窗口仅一对消息流 widget，MainWindow 引用保持。"""
    win = build_real_main_window(qloop, tmp_path)
    assert len(win.findChildren(MessageView)) == 1
    assert len(win.findChildren(MessageDetail)) == 1
    assert win.findChild(MessageView) is win.live_view
    assert win.findChild(MessageDetail) is win.message_detail
    win.close()


def test_live_splitter_above_bottom_row(qapp, qloop, tmp_path):
    """G3-3：实时流 splitter 在统计卡之后；活动/统计左右并排在同一 QHBoxLayout。"""
    win = build_real_main_window(qloop, tmp_path)
    layout = _dashboard_layout(win)

    splitter = win.dashboard.findChild(QSplitter, "dashboardLiveSplitter")
    assert splitter is not None
    assert splitter.indexOf(win.live_view) >= 0
    assert splitter.indexOf(win.message_detail) >= 0

    # 布局中 splitter 的位置 < 底部左右块的 QHBoxLayout 位置
    splitter_pos = layout.indexOf(splitter)
    bottom_pos = next(
        i
        for i in range(layout.count())
        if isinstance(layout.itemAt(i).layout(), QHBoxLayout)
        and layout.itemAt(i).layout() is not None
        and _is_bottom_row(layout.itemAt(i).layout())
    )
    assert 0 <= splitter_pos < bottom_pos

    # 底部行内两个 QGroupBox：最近活动 | 频道统计
    bottom = layout.itemAt(bottom_pos).layout()
    groups = [
        bottom.itemAt(i).widget()
        for i in range(bottom.count())
        if isinstance(bottom.itemAt(i).widget(), QGroupBox)
    ]
    titles = [g.title() for g in groups]
    assert titles == ["最近活动", "频道统计"]
    win.close()


def _is_bottom_row(layout) -> bool:
    return any(
        isinstance(layout.itemAt(i).widget(), QGroupBox)
        and "最近活动" in layout.itemAt(i).widget().title()
        for i in range(layout.count())
    )


def test_media_page_index_is_two():
    """G3-5：媒体页索引 2（原 3），设置页索引 3。"""
    from tgmonitor.ui.nav_bar import _NAV_LABELS_SRC

    assert _NAV_LABELS_SRC == ("大盘", "频道", "媒体", "设置")
    assert _NAV_LABELS_SRC.index("媒体") == 2
