"""v2.0.5 FR-01 + FR-04 契约 — 大盘去"操作台"标题 + 面板标题归位框内。

- G1:大盘不再有 objectName=pageTitle 且文本="操作台" 的 QLabel;
  其他页面仍用全局 #pageTitle 规则(计数不因本改动消失)。
- G4:「最近活动 / 频道统计」两个 QGroupBox 命中 dashPanel 专属 QSS,
  标题归位框内(规则不含 subcontrol-origin: margin),title 文本不变,
  设置页 FormSection 分组框不带 dashPanel(全局规则未被波及)。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QGroupBox

from tests.fixtures._real_window import build_real_main_window

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


def test_dash_panels_have_object_name_and_titles(qapp, qloop, tmp_path):
    """G4:两个面板命中 dashPanel,title 文本保持 2004 契约。"""
    win = build_real_main_window(qloop, tmp_path)
    groups = [g for g in win.dashboard.findChildren(QGroupBox) if g.objectName() == "dashPanel"]
    assert [g.title() for g in groups] == ["最近活动", "频道统计"]
    win.close()
