"""v2.0.1 FIX-02 端到端回归:真实 MainWindow 里点击 LIVE 消息 → 右侧详情。

根因(2026-11-16 真实窗口探针坐实):`MessageEmptyOverlay` 是覆盖整个
MessageView 的可见 QWidget,parent 直接是 `MessageView` 而非 `viewport()`。
真实鼠标点击被它截获后只沿父链传给 MessageView,**不会**传给兄弟
`viewport`,QListView.clicked 永不 emit → message_selected 为 0 →
MessageDetail._current 一直是 None。

早期分段探针直接 `QTest.mouseClick(view.viewport(), ...)` 绕过了 hit-test,
所以误判「overlay 未吞事件」。本文件用 `win.childAt()` 取真实命中 widget
再点击,复现用户真实路径。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPlainTextEdit, QPushButton

from tests.fixtures._factories import make_message
from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.widgets.message_list_model import MessageListModel

_TEXTS = ("第一条消息 ALPHA", "第二条消息 BETA", "第三条消息 GAMMA")


def _seed_live_messages(lv, qapp):
    """注入 3 条消息,返回 {(row, DTO)} 供断言(不假设列表顺序)。"""
    lv.set_messages([make_message(msg_id=i + 1, text=t) for i, t in enumerate(_TEXTS)])
    qapp.processEvents()
    assert lv.count() == len(_TEXTS)
    return lv


def _dto_at(lv, row: int):
    return lv.model().data(lv.model().index(row, 0), MessageListModel.DtoRole)


def _click_row(win, qapp, row: int):
    """按真实 hit-test 口径点击第 row 行,返回该行 DTO。

    `QApplication.widgetAt` 依赖窗口系统坐标,在 offscreen / Windows CI
    上不如 widget 树查询稳定;这里用 `win.childAt()` 取最深层可见子
    widget,等价于 Qt 的鼠标接收者选择。
    """
    lv = win.live_view
    idx = lv.model().index(row, 0)
    rect = lv.visualRect(idx)
    assert rect.isValid() and rect.width() > 0 and rect.height() > 0
    pos_vp = QPoint(
        rect.x() + min(20, rect.width() // 2),
        rect.y() + min(10, rect.height() // 2),
    )
    win_pos = lv.viewport().mapTo(win, pos_vp)
    hit = win.childAt(win_pos)
    assert hit is not None, f"第 {row} 行坐标 {win_pos} 没有可见 widget"

    local = hit.mapFromGlobal(win.mapToGlobal(win_pos))
    QTest.mouseClick(hit, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, local)
    qapp.processEvents()
    return _dto_at(lv, row)


def test_click_live_message_shows_detail_and_switching_updates_it(qapp, qloop, tmp_path):
    """点第 0 行 → 详情指向该 DTO 且正文正确;点第 1 行 → 详情跟着换。"""
    win = build_real_main_window(qloop, tmp_path)
    _seed_live_messages(win.live_view, qapp)
    detail = win.message_detail

    assert detail._current is None, "初始应停在占位面板"

    first = _click_row(win, qapp, 0)
    assert detail._current is first, "点击后详情必须指向被点中的消息 DTO"
    text_edit = detail.findChild(QPlainTextEdit, "detailTextEdit")
    assert text_edit is not None
    assert first.text in text_edit.toPlainText()

    second = _click_row(win, qapp, 1)
    assert second is not first
    assert detail._current is second, "切换行后详情必须更新为新消息"
    text_edit = detail.findChild(QPlainTextEdit, "detailTextEdit")
    assert text_edit is not None
    assert second.text in text_edit.toPlainText()

    # 关闭详情 → 回占位
    close_btn = next(
        (b for b in detail.findChildren(QPushButton) if b.text() == "关闭详情"),
        None,
    )
    assert close_btn is not None
    QTest.mouseClick(close_btn, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert detail._current is None


def test_floating_new_message_button_stays_clickable(qapp, qloop, tmp_path):
    """修复不能顺手废掉「N 条新消息 ↓」浮条。

    `WA_TransparentForMouseEvents` 会让整个 overlay 子树(含浮条)对鼠标
    透明,是一个看似省事但会破坏本用例的错解。
    """
    win = build_real_main_window(qloop, tmp_path)
    lv = _seed_live_messages(win.live_view, qapp)
    overlay = lv._empty_overlay_widget

    clicks: list[int] = []
    overlay.floating_clicked.connect(lambda: clicks.append(1))
    overlay.show_floating_btn()
    qapp.processEvents()

    btn = overlay._floating_btn
    assert btn.isVisible()
    QTest.mouseClick(
        btn, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, btn.rect().center()
    )
    qapp.processEvents()
    assert clicks == [1], "浮条点击必须仍能触发 floating_clicked(view.scrollToTop)"
