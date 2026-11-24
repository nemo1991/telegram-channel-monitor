"""v2.0.7 FR-06/FR-07 契约 — 监听频道管理左右布局 + 行尾加减按钮。

- 两张卡同处一个 QHBoxLayout(搜索框仍独占顶部一行);
- 底部双击提示与双击信号删除;
- 行文本/tooltip 不含 chat_id,id 仍存在 Qt.UserRole;
- 行尾 QToolButton 点击发 qlonglong 信号,64 位负 id 不溢出;
- 门控禁用行按钮;过滤隐藏同步到挂件。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QVBoxLayout

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import EventBus
from tgmonitor.ui.widgets.channel_widget import ChannelListCard, ChannelWidget

_BIG_ID = -1001375475051  # 真实 Telegram chat_id


def _widget(qapp, qloop_16):  # noqa: ANN001 - 测试 helper
    app = MagicMock()
    app.bus = EventBus()

    async def _empty() -> list:
        return []

    app.channels.list_tg_subscribed_channels = _empty
    return ChannelWidget(app, qloop_16)


def _card(icon: str) -> ChannelListCard:
    return ChannelListCard(title="x", action_label="", row_action_icon=icon, show_action=False)


def test_two_cards_side_by_side(qapp, qloop_16) -> None:
    w = _widget(qapp, qloop_16)
    root = w.layout()
    assert isinstance(root, QVBoxLayout)
    rows = [root.itemAt(i).layout() for i in range(root.count())]
    cards_row = next(r for r in rows if isinstance(r, QHBoxLayout))
    widgets = [cards_row.itemAt(i).widget() for i in range(cards_row.count())]
    assert widgets == [w.joined_card, w.subs_card]
    w.deleteLater()


def test_bottom_hint_and_double_click_gone(qapp) -> None:
    card = _card("plus")
    assert [lbl for lbl in card.findChildren(QLabel) if lbl.property("role") == "hint"] == []
    assert not hasattr(card, "item_double_clicked")
    card.deleteLater()


def test_row_text_has_no_chat_id_but_keeps_it_in_data(qapp) -> None:
    card = _card("plus")
    card.set_items(
        [ChannelDTO(id=_BIG_ID, title="科技早报", username="tech", kind="channel")],
        count_template="{n}",
    )
    item = card.lst.item(0)
    assert item is not None
    assert item.text() == "科技早报  @tech"
    assert str(abs(_BIG_ID)) not in item.text()
    assert item.toolTip() == ""
    assert item.data(Qt.UserRole) == _BIG_ID

    row = card.lst.itemWidget(item)
    assert row is not None
    # 2026-11-19 hotfix:文字由 item delegate 独占渲染,挂件 label 留空去重影。
    name = row.findChild(QLabel, "channelRowName")
    assert name is not None and name.text() == ""
    card.deleteLater()


def test_row_action_button_emits_64bit_id(qapp) -> None:
    for icon in ("plus", "minus"):
        card = _card(icon)
        card.set_items([ChannelDTO(id=_BIG_ID, title="x", kind="channel")], count_template="{n}")
        got: list[int] = []
        card.item_action_clicked.connect(got.append)
        row = card.lst.itemWidget(card.lst.item(0))
        assert row is not None
        btn = row.findChild(QToolButton, "channelRowAction")
        assert btn is not None
        assert btn.toolTip() == ("加入监听" if icon == "plus" else "移出监听")
        btn.click()
        assert got == [_BIG_ID]
        card.deleteLater()


def test_row_actions_disabled_follow_gate(qapp, qloop_16) -> None:
    w = _widget(qapp, qloop_16)
    ch = ChannelDTO(id=42, title="x", kind="channel")
    w.joined_card.set_items([ch], count_template="{n}")
    w.subs_card.set_items([ch], count_template="{n}")

    w.set_operations_enabled(False)
    for card in (w.joined_card, w.subs_card):
        row = card.lst.itemWidget(card.lst.item(0))
        btn = row.findChild(QToolButton, "channelRowAction")
        assert not btn.isEnabled()
    w.set_operations_enabled(True)
    for card in (w.joined_card, w.subs_card):
        row = card.lst.itemWidget(card.lst.item(0))
        btn = row.findChild(QToolButton, "channelRowAction")
        assert btn.isEnabled()
    w.deleteLater()


def test_filter_hides_row_widget_too(qapp) -> None:
    card = _card("plus")
    data = {
        1: ChannelDTO(id=1, title="Alpha", kind="channel"),
        2: ChannelDTO(id=2, title="Beta", kind="channel"),
    }
    card.set_items(list(data.values()), count_template="{n}")
    card.apply_filter("alpha", data)
    for i in range(card.lst.count()):
        item = card.lst.item(i)
        row = card.lst.itemWidget(item)
        assert item is not None and row is not None
        assert item.isHidden() == row.isHidden()
    card.deleteLater()


def test_long_channel_name_elided_before_row_action(qapp) -> None:
    """长频道名在行尾按钮左侧省略,文字不再压住「+ / −」图标。"""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QFontMetrics
    from PySide6.QtWidgets import QStyleOptionViewItem

    from tgmonitor.ui.widgets.channel_widget import _ChannelRowDelegate

    card = _card("plus")
    long_title = "来一点医学科学前沿🤩🤩🥺🥺 @CNSmydreamlonglong"
    card.set_items(
        [ChannelDTO(id=_BIG_ID, title=long_title, username="CNSmydreamlonglong", kind="channel")],
        count_template="{n}",
    )
    item = card.lst.item(0)
    assert item is not None
    row = card.lst.itemWidget(item)
    assert row is not None
    btn = row.findChild(QToolButton, "channelRowAction")
    assert btn is not None

    delegate = card.lst.itemDelegate()
    assert isinstance(delegate, _ChannelRowDelegate)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 300, 34)
    text_right = delegate._text_right(option, btn)
    # 按钮宽 + 右边距 4 + spacing 4 必须从行右侧预留出来
    assert text_right <= option.rect.right() - btn.sizeHint().width() - 8

    fm = QFontMetrics(qapp.font())
    available = text_right - 30  # 扣掉图标 + 间距后的文字宽度
    elided = fm.elidedText(item.text(), Qt.ElideRight, available)
    assert elided != item.text()
    assert elided.endswith("…")
    assert fm.horizontalAdvance(elided) <= available
    card.deleteLater()


def test_both_channel_lists_use_eliding_delegate(qapp, qloop_16) -> None:
    from tgmonitor.ui.widgets.channel_widget import _ChannelRowDelegate

    w = _widget(qapp, qloop_16)
    assert isinstance(w.joined_card.lst.itemDelegate(), _ChannelRowDelegate)
    assert isinstance(w.subs_card.lst.itemDelegate(), _ChannelRowDelegate)
    w.deleteLater()
