"""PR #5 LIVE 体验增强测试 — 2026-09-14 v1.7.5。

覆盖场景:
- P0-I: 新消息浮条 — scroll 不在最顶 + rowsInserted → 浮条出现 + 计数累加
- P0-I: scroll 在最顶 → 不显示浮条
- P0-I: 点浮条 → scrollToTop + 清计数 + 隐藏
- P0-I: set_messages → 清浮条(reset 场景)
- P0-L: set_empty_state 切换 3 种 state(no_subscribed / searching / live_empty)
- P0-L: 静态 — style.qss + style_dark.qss 含 #floatingNewMsgBtn selector
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.core.dto import MessageDTO  # noqa: E402
from tgmonitor.ui.widgets.message_view import MessageView  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_msg(cid: int = 1, mid: int = 100, text: str = "hello") -> MessageDTO:
    """构造最小 MessageDTO — MessageView append 所需。"""
    return MessageDTO(
        id=cid * 1_000_000 + mid,
        channel_id=cid,
        telegram_msg_id=mid,
        text=text,
        author="tester",
        date=datetime(2026, 9, 14, 10, 0, 0, tzinfo=UTC),
    )


# ---- P0-I: 新消息浮条 ----


def test_floating_btn_initially_hidden(qapp) -> None:
    """2026-09-14 v1.7.5 PR #5:MessageView 初始化 → 浮条 hidden。"""
    view = MessageView()
    view.show()
    qapp.processEvents()
    assert not view._floating_btn.isVisible()  # noqa: SLF001
    assert view._new_msg_count == 0  # noqa: SLF001


def test_floating_btn_appears_when_scrolled(qapp) -> None:
    """2026-09-14 v1.7.5 PR #5:scroll 不在最顶 + append → 浮条出现 + 计数 = 1。"""
    view = MessageView()
    view.show()
    qapp.processEvents()

    # 先灌 30 条让 list 撑出可滚区域
    for i in range(30):
        view.append(_make_msg(mid=100 + i))
    qapp.processEvents()

    # scroll 到非 0
    view.verticalScrollBar().setValue(20)
    qapp.processEvents()

    # 再 append 一条
    view.append(_make_msg(mid=999))
    qapp.processEvents()

    assert view._floating_btn.isVisible(), "scroll 非顶 + append → 浮条 visible"  # noqa: SLF001
    assert view._new_msg_count == 1  # noqa: SLF001


def test_floating_btn_accumulates_count(qapp) -> None:
    """多次 append → 计数累加。"""
    view = MessageView()
    view.show()
    qapp.processEvents()

    for i in range(30):
        view.append(_make_msg(mid=100 + i))
    qapp.processEvents()
    view.verticalScrollBar().setValue(20)
    qapp.processEvents()

    for i in range(5):
        view.append(_make_msg(mid=900 + i))
    qapp.processEvents()

    assert view._new_msg_count == 5  # noqa: SLF001


def test_floating_btn_does_not_appear_at_top(qapp) -> None:
    """scroll 在最顶 → append → 浮条不出现。"""
    view = MessageView()
    view.show()
    qapp.processEvents()

    # 灌 30 条
    for i in range(30):
        view.append(_make_msg(mid=100 + i))
    qapp.processEvents()

    # scroll 在顶(默认 value=0)
    assert view.verticalScrollBar().value() == 0

    view.append(_make_msg(mid=999))
    qapp.processEvents()

    assert not view._floating_btn.isVisible(), "scroll 在顶 → append 不应显示浮条"


def test_floating_btn_click_scrolls_to_top_and_hides(qapp) -> None:
    """点浮条 → scrollToTop + 清计数 + 隐藏。"""
    view = MessageView()
    view.show()
    qapp.processEvents()

    for i in range(30):
        view.append(_make_msg(mid=100 + i))
    qapp.processEvents()
    view.verticalScrollBar().setValue(20)
    qapp.processEvents()
    view.append(_make_msg(mid=999))
    qapp.processEvents()

    assert view._floating_btn.isVisible()  # noqa: SLF001

    view._on_floating_btn_clicked()  # noqa: SLF001
    qapp.processEvents()

    assert not view._floating_btn.isVisible()  # noqa: SLF001
    assert view._new_msg_count == 0  # noqa: SLF001


def test_set_messages_clears_floating_btn(qapp) -> None:
    """set_messages 是 reset,清空浮条计数。"""
    view = MessageView()
    view.show()
    qapp.processEvents()

    for i in range(30):
        view.append(_make_msg(mid=100 + i))
    qapp.processEvents()
    view.verticalScrollBar().setValue(20)
    qapp.processEvents()
    view.append(_make_msg(mid=999))
    qapp.processEvents()
    assert view._new_msg_count == 1  # noqa: SLF001

    # set_messages 触发 reset
    view.set_messages([_make_msg(mid=42), _make_msg(mid=43)])
    qapp.processEvents()

    assert view._new_msg_count == 0  # noqa: SLF001
    assert not view._floating_btn.isVisible()  # noqa: SLF001


# ---- P0-L: 空状态分型 ----


def test_empty_state_default_is_no_subscribed(qapp) -> None:
    """2026-09-14 v1.7.5 PR #5:默认 state = no_subscribed(启动无订阅)。"""
    view = MessageView()
    view.show()
    qapp.processEvents()
    assert view._empty_state == "no_subscribed"  # noqa: SLF001


def test_set_empty_state_searching(qapp) -> None:
    """set_empty_state("searching") → overlay 显示「无匹配结果」+ 🔍 icon。"""
    view = MessageView()
    view.show()
    qapp.processEvents()
    view.set_empty_state("searching")
    qapp.processEvents()
    from PySide6.QtWidgets import QLabel

    labels = view._empty_overlay.findChildren(QLabel)  # noqa: SLF001
    assert len(labels) >= 2
    # labels[0] = icon, labels[1] = title
    assert labels[0].text() == "🔍"
    assert "匹配" in labels[1].text() or "结果" in labels[1].text()


def test_set_empty_state_no_subscribed(qapp) -> None:
    """set_empty_state("no_subscribed") → overlay 显示「未订阅频道」+ 💬 icon。"""
    view = MessageView()
    view.show()
    qapp.processEvents()
    view.set_empty_state("no_subscribed")
    qapp.processEvents()
    from PySide6.QtWidgets import QLabel

    labels = view._empty_overlay.findChildren(QLabel)  # noqa: SLF001
    assert labels[0].text() == "💬"
    assert "订阅" in labels[1].text() or "频道" in labels[1].text()


def test_set_empty_state_live_empty(qapp) -> None:
    """set_empty_state("live_empty") → overlay 显示「暂无消息」+ 💬 icon。"""
    view = MessageView()
    view.show()
    qapp.processEvents()
    view.set_empty_state("live_empty")
    qapp.processEvents()
    from PySide6.QtWidgets import QLabel

    labels = view._empty_overlay.findChildren(QLabel)  # noqa: SLF001
    assert labels[0].text() == "💬"
    # title 跟 no_subscribed 区分
    assert "暂无" in labels[1].text()


# ---- 静态:QSS 覆盖 ----


def test_qss_has_floating_btn_selector_light() -> None:
    """style.qss 含 #floatingNewMsgBtn selector。"""
    from pathlib import Path

    src = Path("src/tgmonitor/ui/resources/style.qss").read_text(encoding="utf-8")
    assert "floatingNewMsgBtn" in src


def test_qss_has_floating_btn_selector_dark() -> None:
    """style_dark.qss 含 #floatingNewMsgBtn selector。"""
    from pathlib import Path

    src = Path("src/tgmonitor/ui/resources/style_dark.qss").read_text(encoding="utf-8")
    assert "floatingNewMsgBtn" in src
