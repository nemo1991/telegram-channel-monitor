"""v2.0.5 FR-03 步骤一契约 — 消息流 QListView 容器观感与最近活动对齐。

- G3-容器:消息流 QListView 设 streamList objectName,QSS 有专属规则,
  白底/圆角/边框/选中 hover 观感与 QListWidget 一致。
- 交替行色仍由 view.setAlternatingRowColors(True) + delegate palette 绘制,
  本 PR 只改容器观感,不动 delegate(留给 FR-06 PR)。
- 交互不变:只读单选、横向滚动条关闭;点击仍 emit message_selected。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QListView

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.widgets.message_view import MessageView

_QSS_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "tgmonitor" / "ui" / "resources" / "style.qss"
)


def _stream_rule() -> str:
    qss = _QSS_PATH.read_text("utf-8")
    start = qss.index("QListView#streamList {")
    rest = qss[start:]
    end = rest.find("/* =====")
    return rest if end == -1 else rest[:end]


def test_message_view_has_stream_list_object_name(qapp):
    """G3-容器:MessageView 自带 streamList objectName。"""
    view = MessageView()
    assert view.objectName() == "streamList"
    view.deleteLater()


def test_qss_stream_rule_matches_list_widget_look():
    """G3-容器:专属规则含白底圆角边框,与 QListWidget 观感关键属性一致。"""
    rule = _stream_rule()
    assert "QListView#streamList {" in rule
    for prop in ("background: #ffffff", "border: 1px solid #e2e4e9", "border-radius: 6px"):
        assert prop in rule
    # 选中/hover 观感与 QListWidget 一致
    assert "QListView#streamList::item:hover" in rule
    assert "background: #eaf3fc" in rule
    assert "QListView#streamList::item:selected" in rule
    assert "background: #b6d0f0" in rule


def test_view_interaction_semantics_unchanged(qapp, qloop, tmp_path):
    """交互无回归:仍为 QListView、交替行色开、单选、无横向滚动条。"""
    win = build_real_main_window(qloop, tmp_path)
    view = win.live_view
    assert isinstance(view, QListView)
    assert view.alternatingRowColors() is True
    assert view.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    assert view.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    win.close()
