"""v2.0.5 第二轮 UI 精修契约 (2026-11-18) — 4 项反馈。

- P1:实时流「N 条新消息 ↓」浮条彻底移除(view + overlay + QSS)。
- P2:「最近活动 / 频道统计」标题离顶边舒适(::title top/left 内推)。
- P3:两个面板外围四角圆角清晰(border-radius 10 + 标题不压角)。
- P4:实时流与详情接壤直角、外围圆角(#liveWrap 容器 + splitter handle 缝)。

几何断言用真实 MainWindow + 真实 QSS(test 的 qapp 默认不加载全局 QSS)。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGroupBox,
    QPushButton,
    QSplitter,
    QWidget,
)

from tests.fixtures._real_window import build_real_main_window

_QSS_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "tgmonitor" / "ui" / "resources" / "style.qss"
)


def _qss() -> str:
    return _QSS_PATH.read_text("utf-8")


# ---------- P1:浮条移除 ----------


def test_no_floating_new_message_button_anywhere(qapp, qloop, tmp_path):
    """P1:整个窗口不存在 floatingNewMsgBtn / 「条新消息」浮条。"""
    win = build_real_main_window(qloop, tmp_path)
    overlay = win.live_view._empty_overlay_widget

    assert not hasattr(win.live_view, "clear_new_msg_counter")
    assert not hasattr(overlay, "floating_clicked")
    assert not hasattr(overlay, "show_floating_btn")
    assert [
        b for b in overlay.findChildren(QPushButton) if b.objectName() == "floatingNewMsgBtn"
    ] == []
    assert _qss().count("floatingNewMsgBtn") == 0
    win.close()


# ---------- P2 + P3:面板标题与圆角 ----------


def test_panel_title_offsets_and_radius_in_qss():
    """P2/P3:面板 ::title 内推 + 圆角 10px,规则同源。"""
    qss = _qss()
    rule = qss[qss.index("QGroupBox#dashPanel {") : qss.index("/* ===== 列表 ===== */")]
    assert "border-radius: 10px" in rule
    assert "top: 10px" in rule
    assert "left: 6px" in rule
    assert "padding: 40px 14px 12px 14px" in rule


def test_panel_content_below_title_with_real_qss(qapp, qloop, tmp_path):
    """P2:套真实 QSS 后,两个面板内容顶边都在标题带(y≈13)之下。"""
    app = QApplication.instance()
    assert app is not None
    prev = app.styleSheet()
    app.setStyleSheet(_qss().replace("{accent}", "#5b9cf5").replace("{accentHover}", "#4a8be4"))
    try:
        win = build_real_main_window(qloop, tmp_path, width=1280, height=900)
        groups = [g for g in win.dashboard.findChildren(QGroupBox) if g.objectName() == "dashPanel"]
        assert len(groups) == 2
        for g in groups:
            content = [
                c
                for c in g.children()
                if isinstance(c, QWidget) and c.parent() is g and c.isVisible()
            ]
            assert content, f"{g.title()} 无内容控件"
            # padding-top=40,内容顶边须 ≥ 39(1px 容差)
            assert min(c.pos().y() for c in content) >= 39
        win.close()
    finally:
        app.setStyleSheet(prev)


# ---------- P4:接壤直角 + 外围圆角 ----------


def test_live_area_has_rounded_wrapper_and_square_seam(qapp, qloop, tmp_path):
    """P4:liveWrap 圆角容器 + splitter handle 直角缝 + viewport 透明。"""
    win = build_real_main_window(qloop, tmp_path)
    dash = win.dashboard

    wrap = dash.findChild(QFrame, "liveWrap")
    assert wrap is not None
    splitter = dash.findChild(QSplitter, "dashboardLiveSplitter")
    assert splitter is not None
    # splitter 是 liveWrap 的直接子项;两个 widget 仍是 splitter 的直接子项
    assert splitter.parent() is wrap
    assert splitter.indexOf(win.live_view) >= 0
    assert splitter.indexOf(win.message_detail) >= 0
    assert splitter.handleWidth() == 1
    assert splitter.orientation() == Qt.Horizontal

    # 两个 viewport 背景透明(内联 stylesheet),让容器圆角显形
    for view in (win.live_view, win.message_detail):
        vp_ss = view.viewport().styleSheet().replace(" ", "")
        assert "background:transparent" in vp_ss

    qss = _qss()
    wrap_rule = qss[qss.index("QFrame#liveWrap {") : qss.index("/* ===== 消息详情面板 ===== */")]
    assert "border-radius: 10px" in wrap_rule
    assert "QSplitter#dashboardLiveSplitter::handle:horizontal" in wrap_rule
    # 子 widget 不再各自画圆角/边框
    stream_rule = qss[qss.index("QListView#streamList {") : qss.index("QListView#streamList::item")]
    assert "background: transparent" in stream_rule
    assert "border: none" in stream_rule
    win.close()
