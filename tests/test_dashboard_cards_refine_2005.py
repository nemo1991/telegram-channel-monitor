"""v2.0.5 统计卡精修契约 — 去副文本行 + 存储后端 DB/对象等大。

- 每张卡的 VBox 只有两行:标题行 + 数值行,无 role=hint 的副文本 QLabel;
- card_storage 用 set_value_pair 后,DB 与对象两个 QLabel 同处数值行、
  同为 #statValue objectName(同字号),并同时可见;
- 其它卡 set_value 只显示单值(value2_lbl 隐藏);
- 真实窗口 update_stats() 后存储卡确实填入两后端值且可见。
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget, _StatCard


def test_no_sub_hint_row_on_cards(qapp, qloop, tmp_path):
    """F1:卡片不再有副文本行(role=hint 的 QLabel),仅有标题 + 数值。"""
    win = build_real_main_window(qloop, tmp_path)
    dash = win.dashboard
    assert isinstance(dash, DashboardWidget)
    for card in (
        dash.card_channels,
        dash.card_monitored,
        dash.card_state,
        dash.card_storage,
        dash.card_total_messages,
    ):
        assert isinstance(card, _StatCard)
        hints = [lbl for lbl in card.findChildren(QLabel) if lbl.property("role") == "hint"]
        assert hints == [], f"{card.objectName()} 不应再有副文本行"
        assert not hasattr(card, "sub_lbl")
    win.close()


def test_card_layout_has_two_rows(qapp, qloop, tmp_path):
    """F1:卡片根布局 = 标题行(QHBox) + 数值行(QHBox),无第三行。"""
    win = build_real_main_window(qloop, tmp_path)
    card = win.dashboard.card_storage
    vbox = card.layout()
    assert isinstance(vbox, QVBoxLayout)
    rows = [vbox.itemAt(i).layout() for i in range(vbox.count())]
    assert all(isinstance(r, QHBoxLayout) for r in rows)
    assert len(rows) == 2, f"卡片应只有标题行 + 数值行,实际 {len(rows)} 行"
    win.close()


def test_storage_db_and_object_same_size(qapp):
    """F2:存储卡 DB / 对象同为 #statValue 字号、同处数值行并同时可见。"""
    card = _StatCard("存储后端", "settings")
    card.set_value_pair("DB:sqlite", "对象:local")

    assert card.value_lbl.text() == "DB:sqlite"
    assert card.value2_lbl.text() == "对象:local"
    assert card.value_lbl.objectName() == "statValue"
    assert card.value2_lbl.objectName() == "statValue"
    # 同字号:两者 font 一致(同为 #statValue 样式,未设独立字体)
    assert card.value_lbl.font() == card.value2_lbl.font()
    assert card.value_lbl.isVisibleTo(card) or card.value_lbl.isVisible()
    assert card.value2_lbl.isVisibleTo(card) or card.value2_lbl.isVisible()

    # 两者所在的数值行是标题行之后的第二个 QHBoxLayout
    vbox = card.layout()
    assert isinstance(vbox, QVBoxLayout)
    value_row = vbox.itemAt(1).layout()
    assert isinstance(value_row, QHBoxLayout)
    members = [value_row.itemAt(i).widget() for i in range(value_row.count())]
    assert card.value_lbl in members and card.value2_lbl in members


def test_single_value_card_hides_second_label(qapp):
    """F2 边界:普通卡 set_value 只显示单值,第二数值 label 隐藏。"""
    card = _StatCard("已加入频道", "nav_channels")
    card.set_value("12")
    assert card.value_lbl.text() == "12"
    assert not card.value2_lbl.isVisible()


def test_real_window_storage_card_filled_from_settings(qapp, qloop, tmp_path):
    """F2 e2e:真实窗口 update_stats() 后存储卡显示两后端、等大。"""
    win = build_real_main_window(qloop, tmp_path)
    dash = win.dashboard
    assert isinstance(dash, DashboardWidget)
    dash.update_stats(monitored_count=3)

    db_text = dash.card_storage.value_lbl.text()
    obj_text = dash.card_storage.value2_lbl.text()
    assert db_text.startswith("DB:") and len(db_text) > 3
    assert obj_text.startswith("对象:") and len(obj_text) > 3
    assert dash.card_storage.value_lbl.objectName() == dash.card_storage.value2_lbl.objectName()
    win.close()
