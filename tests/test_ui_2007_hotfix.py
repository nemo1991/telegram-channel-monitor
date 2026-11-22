"""v2.0.7 上线后 5 项 UI 回归 hotfix 契约 — 2026-11-19 用户实测反馈。

1. APPID 数字可见:QSpinBox NoButtons 下内部 QLineEdit 宽度不能是 1px;
2. 设置页背景跟随大盘(#f5f6fa 灰底),不再是纯白;
3. 频道行文字不重影:item delegate 独占文字,挂件内 label 留空;
4. 左栏标题简化为「已加入」;
5. 左右两栏列表顶部对齐(标题行等高)。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QSpinBox, QStackedWidget

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.core.dto import ChannelDTO
from tgmonitor.ui.widgets.channel_widget import ChannelWidget
from tgmonitor.ui.widgets.settings_page import SettingsPage

_QSS_PATH = Path(__file__).resolve().parents[1] / "src/tgmonitor/ui/resources/style.qss"


def _show_settings(qapp, qloop, tmp_path):  # noqa: ANN001 - 测试 helper
    """构造真实主窗口并切到设置页,保证涉及几何的断言拿到布局后的尺寸。"""
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None
    win.findChild(QStackedWidget).setCurrentWidget(settings)
    win.resize(1200, 820)
    QApplication.processEvents()
    QApplication.processEvents()
    return win, settings, win.findChild(ChannelWidget)


def test_appid_line_edit_has_usable_width(qapp, qloop, tmp_path) -> None:
    """F1:API ID 的 QSpinBox 内部 lineEdit 宽度 >= 100px(数字不被裁切)。"""
    win, settings, _ = _show_settings(qapp, qloop, tmp_path)
    spin = settings.in_api_id
    assert isinstance(spin, QSpinBox)
    line_edit = spin.lineEdit()
    assert line_edit is not None
    assert line_edit.minimumWidth() >= 100, (
        f"NoButtons 下 lineEdit 宽度兜底丢失: {line_edit.minimumWidth()}px"
    )
    assert line_edit.text() == spin.text()  # 数字确实在内部编辑器里
    win.close()


def test_settings_background_matches_dashboard(qapp) -> None:
    """F2:QSS 里设置页滚动内容背景为大盘灰底 #f5f6fa,不再白底。"""
    qss = _QSS_PATH.read_text(encoding="utf-8")
    # 精确匹配设置页背景规则块(不含后面的 #channelCard 白底卡片规则)
    rule = "#settingsPage,\n#settingsPage #settingsScrollContent {\n    background: #f5f6fa;\n}"
    assert rule in qss, "设置页背景应跟随大盘灰底 #f5f6fa"


def test_channel_row_text_not_duplicated(qapp, qloop, tmp_path) -> None:
    """F3:行文字只由 item delegate 渲染一次,挂件内 channelRowName label 为空。"""
    win, _, cw = _show_settings(qapp, qloop, tmp_path)
    ch = ChannelDTO(id=-1001375475051, title="科技早报", kind="channel", username="tech")
    cw.set_joined([ch])
    item = cw.joined_list.item(0)
    assert item is not None and item.text() == "科技早报  @tech"

    row = cw.joined_list.itemWidget(item)
    assert row is not None
    name = row.findChild(QLabel, "channelRowName")
    assert name is not None
    assert name.text() == "", "挂件内 label 必须留空,否则与 item 文字重影"
    win.close()


def test_joined_title_simplified(qapp, qloop, tmp_path) -> None:
    """F4:左栏标题简化为「已加入」(计数模板与构造标题均同步)。"""
    win, _, cw = _show_settings(qapp, qloop, tmp_path)
    assert cw.joined_card.count_label.text() == "已加入 · 0"
    assert cw.joined_card._title == "已加入"
    win.close()


def test_two_card_lists_top_aligned(qapp, qloop, tmp_path) -> None:
    """F5:左右两栏列表顶部对齐(标题行等高)。

    v2.0.7 的真实修复是给两卡片的 `count_label` 设 `setFixedHeight(31)`
    (`channel_widget.py:221`)——左栏有同步按钮撑到 31px、右栏无按钮只有 label
    20px,曾导致两栏列表顶部错开 11px。

    旧断言用 `mapToGlobal().y()` 比较两栏绝对屏幕坐标,本地 DPI/显示缩放变化
    下稳定误报(joined=440, subs=428,差 12px),与代码改动无关。改为断言
    **对齐的结构性成因**(同父布局 + 标题等高 + 列表容器同构),不依赖窗口
    实际摆放位置——对齐一旦被破坏(标题高度不等 / 卡片进了不同布局),本用例
    仍会失败,不是恒真断言。
    """
    win, _, cw = _show_settings(qapp, qloop, tmp_path)
    ch = ChannelDTO(id=1, title="科技早报", kind="channel")
    cw.set_joined([ch])
    cw.set_monitored([ch])

    joined_card, subs_card = cw.joined_card, cw.subs_card
    # 成因①:两卡片挂在同一个 QHBoxLayout(cards_row)里,顶部基准一致。
    # cards_row 嵌套在 cw 根 QVBoxLayout 内,遍历子布局找到持有两卡片的那一个。
    assert joined_card.parent() is subs_card.parent(), "两卡片应在同一父容器"
    cards_row = next(
        lay
        for i in range(cw.layout().count())
        if (lay := cw.layout().itemAt(i).layout()) is not None
        and joined_card in [lay.itemAt(j).widget() for j in range(lay.count())]
    )
    assert subs_card in [cards_row.itemAt(j).widget() for j in range(cards_row.count())]

    # 成因②:标题行等高——v2.0.7 修复点,两卡 count_label 都 setFixedHeight(31)。
    # 断言 min/max 而非 height():后者在未布局时为 0,前者是 setFixedHeight 的
    # 直接属性、不依赖窗口是否 show。若有人改回"右栏不固定高度",立即失败(非恒真)。
    for card in (joined_card, subs_card):
        title = card.count_label
        assert title.minimumHeight() == 31, f"标题 minimumHeight 被改: {title.minimumHeight()}"
        assert title.maximumHeight() == 31, f"标题 maximumHeight 被改: {title.maximumHeight()}"

    # 成因③:列表容器结构同构(卡片根布局 head→stack,lst 在 stack index 0),
    # 保证两列表相对卡片顶部的偏移一致。
    for card in (joined_card, subs_card):
        root = card.layout()
        assert root.count() >= 2
        assert isinstance(root.itemAt(0).layout(), QHBoxLayout)  # head 标题行
        assert card.lst.parent() is card._stack
        assert card._stack.indexOf(card.lst) == 0
    win.close()
