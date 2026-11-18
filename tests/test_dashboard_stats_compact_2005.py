"""v2.0.5 FR-02 契约 — 压缩顶部统计卡,把纵向空间让给消息流。

- G2(几何):固定 1280×800 窗口下,卡片行 100→68px、消息流 splitter
  318→356px(不减)、root margin/spacing 与 cards spacing 收紧、stretch 3:2 不变。
- 5 张卡与取数口径不变(不精简、不改 DTO)。
- 压缩后文字不裁切(QFontMetrics 校验最小高度)。
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QSplitter

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget

# 改造前基线(同 fixture、同 1280×800 窗口,见 PRD §7 FR-02 / 附录 A)。
# 注意:绝对像素值跨平台不可比(Windows 字体度量下 splitter 比 macOS 矮约
# 40px),故只用代码写死的固定高度(卡片 100→68)做绝对值断言;消息流"变高"
# 用 splitter/card 比值表达——改造前 318/100≈3.18,改造后 4.6~5.2。
_BASELINE_CARD_H = 100


def _cards_row(dash: DashboardWidget) -> QHBoxLayout:
    lay = dash.layout()
    for i in range(lay.count()):
        sub = lay.itemAt(i).layout()
        if isinstance(sub, QHBoxLayout) and sub.count() == 5:
            return sub
    raise AssertionError("找不到 5 卡统计行")


def test_cards_compressed_and_splitter_grows(qapp, qloop, tmp_path):
    """G2:卡片行变矮、消息流 splitter 不减反增(固定尺寸几何断言)。"""
    win = build_real_main_window(qloop, tmp_path, width=1280, height=800)
    dash = win.dashboard
    assert isinstance(dash, DashboardWidget)

    row = _cards_row(dash)
    card = dash.card_channels
    assert card.height() < _BASELINE_CARD_H
    assert card.height() <= 68
    assert row.geometry().height() <= 68

    sp = dash.findChild(QSplitter, "dashboardLiveSplitter")
    assert sp is not None
    # 用比值而非绝对像素:改造前 splitter/card≈3.18,改造后 ≥4.6(跨平台稳定)。
    # 避免把某个平台的绝对基线写死导致其他平台误报。
    assert sp.height() > card.height() * 4  # 释放的空间给了消息流
    win.close()


def test_root_spacing_tightened(qapp, qloop, tmp_path):
    """G2:root margin/spacing 与 cards spacing 收紧。"""
    win = build_real_main_window(qloop, tmp_path, width=1280, height=800)
    dash = win.dashboard
    lay = dash.layout()
    m = lay.contentsMargins()
    assert (m.left(), m.top(), m.right(), m.bottom()) == (16, 16, 16, 16)
    assert lay.spacing() == 12
    assert _cards_row(dash).spacing() == 10
    win.close()


def test_stretch_ratio_unchanged(qapp, qloop, tmp_path):
    """G2:live_splitter 与 bottom_row 的 stretch 保持 3:2。"""
    win = build_real_main_window(qloop, tmp_path, width=1280, height=800)
    lay = win.dashboard.layout()
    # item[0]=cards(无 stretch)、item[1]=splitter(3)、item[2]=bottom_row(2)
    assert lay.stretch(1) == 3
    assert lay.stretch(2) == 2
    win.close()


def test_five_cards_and_values_intact(qapp, qloop, tmp_path):
    """口径不变:仍为 5 张卡,取值接口可用(2026-11-18 起无副文本行)。"""
    win = build_real_main_window(qloop, tmp_path, width=1280, height=800)
    dash = win.dashboard
    row = _cards_row(dash)
    assert [row.itemAt(i).widget() for i in range(5)] == [
        dash.card_channels,
        dash.card_subscribed,
        dash.card_state,
        dash.card_storage,
        dash.card_total_messages,
    ]
    # set_value 仍工作(压缩未破坏卡片取值契约);底部副文本行已移除。
    dash.card_channels.set_value("12")
    assert dash.card_channels.value_lbl.text() == "12"
    assert not hasattr(dash.card_channels, "sub_lbl")
    win.close()


def test_compressed_card_text_not_clipped(qapp, qloop, tmp_path):
    """压缩后文字不裁切:卡内布局的最小所需高度 ≤ 卡片固定高度。

    用 Qt 布局自身的 minimumSize（已含各平台字体度量与 margins/spacing）：
    比手写 QFontMetrics 行高求和更贴合真实排版，且跨平台稳定。
    """
    win = build_real_main_window(qloop, tmp_path, width=1280, height=800)
    dash = win.dashboard
    card = dash.card_channels
    card.set_value("1234")
    vbox = card.layout()
    assert vbox is not None
    assert vbox.totalMinimumSize().height() <= card.height()
    win.close()
