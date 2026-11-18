"""v2.0.5 FR-01 + FR-04 契约 — 大盘去"操作台"标题 + 面板标题归位框内。

- G1:大盘不再有 objectName=pageTitle 且文本="操作台" 的 QLabel;
  其他页面仍用全局 #pageTitle 规则(计数不因本改动消失)。
- G4:「最近活动 / 频道统计」两个 QGroupBox 命中 dashPanel 专属 QSS,
  标题归位框内(规则不含 subcontrol-origin: margin),title 文本不变,
  设置页 FormSection 分组框不带 dashPanel(全局规则未被波及)。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QGroupBox, QLabel

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget

_QSS_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "tgmonitor"
    / "ui"
    / "resources"
    / "style.qss"
)


def _dash_panel_rule() -> str:
    """取 QGroupBox#dashPanel 专属规则块(含其 ::title 子规则)。"""
    qss = _QSS_PATH.read_text("utf-8")
    start = qss.index("QGroupBox#dashPanel")
    # 规则块到下一个 /* 注释或 == 分节标记为止
    rest = qss[start:]
    end = rest.find("/* =====")
    return rest if end == -1 else rest[:end]


def test_dashboard_has_no_page_title_qlabel(qapp, qloop, tmp_path):
    """G1:大盘内不存在"操作台"标题节点。"""
    win = build_real_main_window(qloop, tmp_path)
    assert isinstance(win.dashboard, DashboardWidget)
    hits = [
        lbl
        for lbl in win.dashboard.findChildren(QLabel)
        if lbl.text() == "操作台" and lbl.objectName() == "pageTitle"
    ]
    assert hits == []
    win.close()


def test_global_pagetitle_qss_rule_preserved():
    """G1:全局 #pageTitle 规则保留(其他页面仍依赖),没被顺手删掉。"""
    qss = _QSS_PATH.read_text("utf-8")
    assert "#pageTitle {" in qss


def test_dash_panels_have_object_name_and_titles(qapp, qloop, tmp_path):
    """G4:两个面板命中 dashPanel,title 文本保持 2004 契约。"""
    win = build_real_main_window(qloop, tmp_path)
    groups = [
        g for g in win.dashboard.findChildren(QGroupBox) if g.objectName() == "dashPanel"
    ]
    assert [g.title() for g in groups] == ["最近活动", "频道统计"]
    win.close()


def test_dash_panel_qss_puts_title_inside_frame():
    """G4:专属规则不含 margin origin(标题归位框内),padding-top 收紧。"""
    rule = _dash_panel_rule()
    assert "QGroupBox#dashPanel {" in rule
    assert "QGroupBox#dashPanel::title {" in rule
    assert "subcontrol-origin: margin" not in rule
    assert "margin-top: 0" in rule
    assert "subcontrol-origin: padding" in rule
    # padding-top 收紧到 ≤12px(原全局规则为 20px)
    assert "padding: 10px" in rule


def test_settings_page_groups_are_not_dash_panel(qapp, qloop, tmp_path):
    """G4:设置页分组框不带 dashPanel objectName(全局 QGroupBox 规则未动)。"""
    win = build_real_main_window(qloop, tmp_path)
    # 全局 QGroupBox 规则仍在(未误删/未改 margin origin)
    qss = _QSS_PATH.read_text("utf-8")
    global_block = qss[qss.index("QGroupBox {") : qss.index("QGroupBox::title")]
    assert "margin-top: 14px" in global_block
    assert "padding: 20px 14px 14px 14px" in global_block
    # 设置页内不应出现 dashPanel(专属规则只命中大盘两块)
    settings = win.stack.widget(3)
    assert settings is not None
    assert [g for g in settings.findChildren(QGroupBox) if g.objectName() == "dashPanel"] == []
    win.close()
