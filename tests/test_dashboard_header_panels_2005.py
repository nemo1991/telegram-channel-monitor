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
    Path(__file__).resolve().parent.parent / "src" / "tgmonitor" / "ui" / "resources" / "style.qss"
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
    groups = [g for g in win.dashboard.findChildren(QGroupBox) if g.objectName() == "dashPanel"]
    assert [g.title() for g in groups] == ["最近活动", "频道统计"]
    win.close()


def test_dash_panel_qss_puts_title_inside_frame():
    """G4:专属规则不含 margin origin(标题归位框内),padding-top 留出标题行。"""
    rule = _dash_panel_rule()
    assert "QGroupBox#dashPanel {" in rule
    assert "QGroupBox#dashPanel::title {" in rule
    assert "subcontrol-origin: margin" not in rule
    assert "margin-top: 0" in rule
    assert "subcontrol-origin: padding" in rule
    # 2026-11-18:padding-top 10→28,给标题单独留出一行,列表不再盖住标题。
    assert "padding: 28px 12px 10px 12px" in rule


def test_panel_content_starts_below_title(qapp, qloop, tmp_path):
    """2026-11-18:两个面板的列表/表头顶部都在标题下方,不重叠。

    取 QSS padding-top 作为标题占据的垂直带高;子控件(列表/表头)相对
    QGroupBox 的 y 必须 ≥ 该值,才不会盖住标题文字。空列表也成立(布局
    与内容无关)。
    """
    # test 的 qapp 不加载全局 QSS(那在 app.py 里做),这里临时套上真实样式再量几何,
    # 测完恢复 —— 否则拿到的是 Qt 默认样式(标题带 21px),测不到 dashPanel 规则。
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication.instance()
    assert app is not None
    prev_qss = app.styleSheet()
    app.setStyleSheet(
        _QSS_PATH.read_text("utf-8")
        .replace("{accent}", "#5b9cf5")
        .replace("{accentHover}", "#4a8be4")
    )
    try:
        win = build_real_main_window(qloop, tmp_path, width=1280, height=900)
        dash = win.dashboard
        groups = [g for g in dash.findChildren(QGroupBox) if g.objectName() == "dashPanel"]
        assert len(groups) == 2

        # 从专属 QSS 规则解析 padding-top(避免测试里写死像素,和样式同源)
        import re

        rule = _dash_panel_rule()
        m = re.search(r"padding:\s*(\d+)px", rule)
        assert m is not None
        pad_top = int(m.group(1))

        for g in groups:
            # 只取 parent 直接是 g 的可见内容控件(布局放进来的列表/表格);
            # pos() 是相对父控件 g 的坐标,避免 mapFrom 对 QObject 子类报错。
            content_widgets = [
                c
                for c in g.children()
                if isinstance(c, QWidget) and c.parent() is g and c.isVisible()
            ]
            assert content_widgets, f"{g.title()} 面板无内容控件"
            child_tops = [c.pos().y() for c in content_widgets]
            # 顶边必须落在 padding-top 之下(容纳标题的一行)。留 1px border 容差。
            assert min(child_tops) >= pad_top - 1, (
                f"{g.title()} 内容顶部 {min(child_tops)}px 侵入标题带 (padding-top={pad_top}px)"
            )
        win.close()
    finally:
        app.setStyleSheet(prev_qss)


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
