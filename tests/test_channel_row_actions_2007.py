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
from PySide6.QtWidgets import QToolButton

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
