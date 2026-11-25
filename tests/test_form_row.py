"""form_row helper 单元测试 — src/tgmonitor/ui/widgets/form_row.py。

QT_QPA_PLATFORM=offscreen 无 GUI 跑(跟其他 UI 测试一致)。
"""

from __future__ import annotations

from enum import Enum

import pytest
from PySide6.QtWidgets import QApplication, QFormLayout, QWidget

from tgmonitor.ui.widgets.form_row import (
    combo_field,
)

# `qapp` from tests/conftest.py — PR 1a 已提供 session-scope 单例


@pytest.fixture
def form_holders(qapp: QApplication) -> tuple[QWidget, QFormLayout]:
    """返 (widget, layout) — widget 由 fixture 持有,避免 GC QFormLayout。

    pytest fixture return tuple,caller 解构成 widget, layout 都能用。
    """
    widget = QWidget()
    layout = QFormLayout(widget)
    return widget, layout


@pytest.fixture
def form(form_holders: tuple[QWidget, QFormLayout]) -> QFormLayout:
    _, layout = form_holders
    return layout


# ---- text_field ----


# ---- path_field ----


# ---- combo_field ----


class _Color(Enum):
    RED = "red"
    BLUE = "blue"
    GREEN = "green"


def test_combo_field_disables_wheel_selection(form: QFormLayout) -> None:
    """滚轮经过下拉框不得改选中项(2026-08-18 修复)。

    设置页在 QScrollArea 里,滚动页面时滚轮悬停在下拉框上会无意识地切换
    「数据库后端 / 对象存储后端」,保存后静默覆盖 .env(实测 PG 被滚成
    JSONL)。`_NoWheelComboBox` 重写 wheelEvent 忽略滚轮,选中项保持。
    """
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    cmb = combo_field(form, "Backend:", _Color)
    cmb.setCurrentIndex(1)
    assert cmb.currentData() == _Color.BLUE
    # 模拟滚轮向下滚一格(angleDelta.y=120):默认 QComboBox 会切到下一项,
    # 我们的实现忽略该事件,选中项必须保持。
    ev = QWheelEvent(
        QPointF(0, 0),
        QPointF(0, 0),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    cmb.wheelEvent(ev)
    assert cmb.currentData() == _Color.BLUE
    assert cmb.currentIndex() == 1


# ---- spin_field ----


# ============================================================
# empty_hint — 「暂无内容」占位面板 helper
# ============================================================


def _grab_labels(widget) -> list:
    """helper — 找到 widget 子树里所有 QLabel,验证 icon/title/hint 三行。"""
    from PySide6.QtWidgets import QLabel

    out = []
    for child in widget.findChildren(QLabel):
        out.append(child)
    return out
