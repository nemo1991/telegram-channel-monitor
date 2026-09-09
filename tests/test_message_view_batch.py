"""2026-09-08 v1.7.0:MessageView 多选 + 右键菜单测试。

覆盖:
- selection_mode == ExtendedSelection(非 SingleSelection)
- selection_count_changed 随选择变化 emit
- selection_messages_changed 携带 list[(cid, mid)]
- selected_messages() / selection_count() helper 正确
- clear_selection() 清空选择
- 右键菜单触发 export_requested / delete_requested / mark_read_requested
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QItemSelectionModel, QPoint
from PySide6.QtWidgets import QAbstractItemView, QApplication

from tgmonitor.core.dto import MessageDTO
from tgmonitor.ui.widgets.message_view import MessageView


@pytest.fixture(scope="session")
def qapp():
    """Session-scope QApplication — 与 test_message_view.py 共享。"""
    return QApplication.instance() or QApplication([])


@pytest.fixture
def view_with_messages(qapp) -> MessageView:
    """建一个 MessageView + 注入 5 条消息。"""
    view = MessageView()
    msgs = [
        MessageDTO(id=i, channel_id=1, telegram_msg_id=100 + i, text=f"msg{i}") for i in range(1, 6)
    ]
    view.set_messages(msgs)
    return view


def _select_row(sm: QItemSelectionModel, view: MessageView, row: int) -> None:
    """Helper — Select | Rows 旗标选中一行(Ctrl+Click 风格)。"""
    sm.select(
        view.model().index(row, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )


def test_selection_mode_is_extended(view_with_messages: MessageView) -> None:
    """默认 ExtendedSelection(支持 Ctrl/Shift 多选)— 替代旧的 SingleSelection。"""
    assert view_with_messages.selectionMode() == QAbstractItemView.ExtendedSelection


def test_selection_count_emits_on_change(view_with_messages: MessageView) -> None:
    """Ctrl+Click 选 2 条 → selection_count_changed(2) emit。"""
    view = view_with_messages
    counts: list[int] = []
    view.selection_count_changed.connect(counts.append)

    sm = view.selectionModel()
    assert sm is not None
    _select_row(sm, view, 0)
    _select_row(sm, view, 1)
    assert 2 in counts
    assert view.selection_count() == 2


def test_selection_messages_emits_pairs(view_with_messages: MessageView) -> None:
    """selection_messages_changed 携带 (cid, mid) 列表 — 多条按选中顺序。"""
    view = view_with_messages
    pairs: list = []
    view.selection_messages_changed.connect(pairs.append)

    sm = view.selectionModel()
    assert sm is not None
    for row in range(3):
        _select_row(sm, view, row)

    assert len(pairs) > 0
    last = pairs[-1]
    assert isinstance(last, list)
    # MessageListModel 倒序(newest-first):row 0=msg5=mid=105,
    # row 1=msg4=mid=104, row 2=msg3=mid=103
    assert {(c, m) for c, m in last} == {(1, 105), (1, 104), (1, 103)}


def test_clear_selection_empties_model(view_with_messages: MessageView) -> None:
    """clear_selection() → selection_count()==0 + 触发 0 信号。"""
    view = view_with_messages
    sm = view.selectionModel()
    assert sm is not None
    _select_row(sm, view, 0)
    _select_row(sm, view, 2)
    assert view.selection_count() == 2
    view.clear_selection()
    assert view.selection_count() == 0


def test_selected_messages_filters_non_dto(view_with_messages: MessageView) -> None:
    """selected_messages() 只返 DTO 类型(过滤 None / 异常)— 防御性。"""
    view = view_with_messages
    sm = view.selectionModel()
    assert sm is not None
    _select_row(sm, view, 0)
    _select_row(sm, view, 4)
    sel = view.selected_messages()
    assert isinstance(sel, list)
    assert len(sel) == 2
    assert all(isinstance(t, tuple) and len(t) == 2 for t in sel)
    # row 0 倒序 → mid=105,row 4 → mid=101
    assert set(sel) == {(1, 101), (1, 105)}


def test_right_click_menu_triggers_export_signal(view_with_messages: MessageView) -> None:
    """右键菜单 → 点「导出…」→ export_requested 携带 selection。"""
    view = view_with_messages
    sm = view.selectionModel()
    assert sm is not None
    _select_row(sm, view, 0)

    exports: list = []
    view.export_requested.connect(exports.append)

    # 模拟右键菜单事件 — 通过 monkeypatch menu.exec_ 拿 QPoint
    from PySide6.QtWidgets import QMenu

    captured: dict = {}

    def fake_exec(self, point=None):
        captured["point"] = point
        # 自动触发「导出」action(模拟用户点)
        for action in self.actions():
            if action.text() == view.tr("导出…"):
                action.trigger()
                break
        return None

    # monkeypatch QMenu.exec_ (instance-level via class)
    original_exec = QMenu.exec_
    QMenu.exec_ = fake_exec  # type: ignore[assignment]
    try:
        # 通过 mousePressEvent 触发 contextMenuEvent(RightButton)
        from PySide6.QtGui import QContextMenuEvent

        event = QContextMenuEvent(
            QContextMenuEvent.Mouse, QPoint(10, 10), view.viewport().mapToGlobal(QPoint(10, 10))
        )
        view.contextMenuEvent(event)
    finally:
        QMenu.exec_ = original_exec  # type: ignore[assignment]

    assert len(exports) == 1
    # row 0 = msg5 = mid=105(MessageListModel 倒序)
    assert {(c, m) for c, m in exports[0]} == {(1, 105)}


def test_right_click_menu_triggers_delete_and_mark_read(view_with_messages: MessageView) -> None:
    """右键菜单 → 触发 delete_requested / mark_read_requested。"""
    view = view_with_messages
    sm = view.selectionModel()
    assert sm is not None
    _select_row(sm, view, 0)
    _select_row(sm, view, 2)

    deletes: list = []
    reads: list = []
    view.delete_requested.connect(deletes.append)
    view.mark_read_requested.connect(reads.append)

    from PySide6.QtWidgets import QMenu

    def fake_exec(self, point=None):
        for action in self.actions():
            text = action.text()
            if text == view.tr("删除") or text == view.tr("标记已读"):
                action.trigger()
        return None

    original_exec = QMenu.exec_
    QMenu.exec_ = fake_exec  # type: ignore[assignment]
    try:
        from PySide6.QtGui import QContextMenuEvent

        event = QContextMenuEvent(
            QContextMenuEvent.Mouse,
            QPoint(10, 10),
            view.viewport().mapToGlobal(QPoint(10, 10)),
        )
        view.contextMenuEvent(event)
    finally:
        QMenu.exec_ = original_exec  # type: ignore[assignment]

    assert len(deletes) == 1
    assert len(reads) == 1
    # 2 条 (cid, mid) tuple
    assert len(deletes[0]) == 2
    assert len(reads[0]) == 2


def test_selected_messages_empty_when_no_selection(view_with_messages: MessageView) -> None:
    """无选择 → selected_messages() 返 [];selection_count() 返 0。"""
    view = view_with_messages
    assert view.selected_messages() == []
    assert view.selection_count() == 0
