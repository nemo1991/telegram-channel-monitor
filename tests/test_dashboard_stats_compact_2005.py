"""v2.0.5 FR-02 契约 — 压缩顶部统计卡,把纵向空间让给消息流。

- G2(几何):固定 1280×800 窗口下,卡片行 100→68→48px、消息流 splitter
  318→356px(不减)、root margin/spacing 与 cards spacing 收紧、stretch 3:2 不变。
- v2.0.7 FR-01:「消息总数」卡删除(信息已在频道统计表),剩 4 张单行卡。
- 压缩后文字不裁切(QFontMetrics 校验最小高度)。
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout

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
        if isinstance(sub, QHBoxLayout) and sub.count() == 4:
            return sub
    raise AssertionError("找不到 4 卡统计行")


def test_four_cards_and_values_intact(qapp, qloop, tmp_path):
    """v2.0.7 FR-01:删「消息总数」后剩 4 张卡,取值接口不变。"""
    win = build_real_main_window(qloop, tmp_path, width=1280, height=800)
    dash = win.dashboard
    row = _cards_row(dash)
    assert [row.itemAt(i).widget() for i in range(4)] == [
        dash.card_channels,
        dash.card_monitored,
        dash.card_state,
        dash.card_storage,
    ]
    assert not hasattr(dash, "card_total_messages")
    # set_value 仍工作(压缩未破坏卡片取值契约);底部副文本行已移除。
    dash.card_channels.set_value("12")
    assert dash.card_channels.value_lbl.text() == "12"
    assert not hasattr(dash.card_channels, "sub_lbl")
    win.close()
