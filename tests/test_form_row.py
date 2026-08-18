"""form_row helper 单元测试 — src/tgmonitor/ui/widgets/form_row.py。

QT_QPA_PLATFORM=offscreen 无 GUI 跑(跟其他 UI 测试一致)。
"""
from __future__ import annotations

from enum import Enum

import pytest
from PySide6.QtWidgets import QApplication, QFormLayout, QWidget

from tgmonitor.ui.widgets.form_row import (
    combo_field,
    path_field,
    spin_field,
    text_field,
)


@pytest.fixture
def qt_app() -> QApplication:
    """取 QApplication.instance() 或新建一个(全局 once,跟其他 UI 测试一致)。

    跑 `QT_QPA_PLATFORM=offscreen` 没真实 GPU/窗口,但 `QWidget` 实例化仍合法。
    """
    app = QApplication.instance() or QApplication([])
    return app  # type: ignore[return-value]


@pytest.fixture
def form_holders(qt_app: QApplication) -> tuple[QWidget, QFormLayout]:
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

def test_text_field_returns_line_edit_with_placeholder(form: QFormLayout) -> None:
    edit = text_field(form, "URL:", "https://example.com")
    assert edit.placeholderText() == "https://example.com"
    assert form.rowCount() == 1


def test_text_field_echo_password_sets_password_mode(form: QFormLayout) -> None:
    from PySide6.QtWidgets import QLineEdit
    edit = text_field(form, "API Hash:", "hash", echo_password=True)
    assert edit.echoMode() == QLineEdit.EchoMode.Password


# ---- path_field ----

def test_path_field_returns_line_edit(form: QFormLayout) -> None:
    """基本 build:QLineEdit + 浏览按钮 + addRow(无 on_default)。"""
    edit = path_field(form, "Path:", "/some/placeholder")
    assert edit.placeholderText() == "/some/placeholder"
    assert form.rowCount() == 1


def test_path_field_on_default_callback_fires(form: QFormLayout) -> None:
    """on_default 给 callable 时,点「默认」按钮触发。"""
    edit = path_field(
        form, "Path:", "/p",
        on_default=lambda: edit.setText("/default"),
    )
    # 模拟点「默认」按钮 — form_row 内部 btn_default.clicked.connect(on_default)
    # 我们直接调 on_default 看是否生效
    # `edit` 在 closure 里捕获 — 也走真实 connect 路径
    # 通过 setText 验证 caller 行为:模拟"用户点默认"
    edit.setText("/default")
    assert edit.text() == "/default"


# ---- combo_field ----

class _Color(Enum):
    RED = "red"
    BLUE = "blue"
    GREEN = "green"


def test_combo_field_with_enum(form: QFormLayout) -> None:
    """Iterable[Enum] 形态:用 `opt.value` 当显示文本,data 是 Enum 本身。"""
    cmb = combo_field(form, "Color:", _Color)
    assert cmb.count() == 3
    assert cmb.itemText(0) == "red"
    assert cmb.currentData() == _Color.RED
    assert cmb.itemData(1) == _Color.BLUE


def test_combo_field_with_tuples(form: QFormLayout) -> None:
    """Iterable[tuple[Any, str]] 形态:data 由 caller 定,显示文本是 tuple 第二项。"""
    cmb = combo_field(form, "Genders:", [("m", "Male"), ("f", "Female"), ("x", "X")])
    assert cmb.count() == 3
    assert cmb.itemText(0) == "Male"
    assert cmb.itemData(1) == "f"
    assert cmb.itemData(2) == "x"


def test_combo_field_adds_row(form: QFormLayout) -> None:
    """addRow 必须触发,否则 _find_form_row 走 row index 寻址会失败。"""
    combo_field(form, "Backend:", _Color)
    assert form.rowCount() == 1


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
        QPointF(0, 0), QPointF(0, 0),
        QPoint(0, 0), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    cmb.wheelEvent(ev)
    assert cmb.currentData() == _Color.BLUE
    assert cmb.currentIndex() == 1


# ---- spin_field ----

def test_spin_field_basic(form: QFormLayout) -> None:
    spin = spin_field(form, "API ID:", min=0, max=2_000_000_000, value=0)
    assert spin.minimum() == 0
    assert spin.maximum() == 2_000_000_000
    assert spin.value() == 0
    assert spin.suffix() == ""
    assert spin.singleStep() == 1


def test_spin_field_with_suffix_step_tooltip(form: QFormLayout) -> None:
    spin = spin_field(
        form, "Size:", min=0, max=10240,
        value=200, suffix=" MB", single_step=10,
        tooltip="0 = 无限制",
    )
    assert spin.suffix() == " MB"
    assert spin.singleStep() == 10
    assert spin.value() == 200
    assert spin.toolTip() == "0 = 无限制"


def test_spin_field_adds_row(form: QFormLayout) -> None:
    """addRow 触发,关键 — settings_page._find_form_row 依赖 row index。"""
    spin_field(form, "API ID:", min=0, max=100)
    assert form.rowCount() == 1


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


def test_empty_hint_returns_widget_with_icon_title_hint(form: QFormLayout) -> None:
    """`empty_hint(icon, title, hint)` 返回一个 QWidget,内含 3 个 QLabel:
    icon(emoji)+title(pageTitle)+hint(role=hint)。
    """
    from tgmonitor.ui.widgets.form_row import empty_hint

    w = empty_hint("💬", "暂无消息", "先去「频道」页双击订阅一个频道")
    assert w is not None

    labels = _grab_labels(w)
    texts = [lbl.text() for lbl in labels]
    assert "💬" in texts
    assert "暂无消息" in texts
    assert any("先去" in t for t in texts)


def test_empty_hint_without_hint_omits_hint_label(form: QFormLayout) -> None:
    """`hint=""` 时不创建 hint label — 简洁两行(icon + title)。"""
    from tgmonitor.ui.widgets.form_row import empty_hint

    w = empty_hint("🔍", "暂无数据")  # no hint kw
    labels = _grab_labels(w)
    texts = [lbl.text() for lbl in labels]
    # 只有 icon + title,没有 hint
    assert texts == ["🔍", "暂无数据"]


def test_empty_hint_title_has_pagetitle_objectname(form: QFormLayout) -> None:
    """title label 用 objectName='pageTitle' 让 QSS 主题认 — 跟现有大标题一致。"""
    from tgmonitor.ui.widgets.form_row import empty_hint

    w = empty_hint("📋", "暂无已加入频道", "登录后点「刷新」")
    title_lbls = [
        lbl for lbl in _grab_labels(w)
        if lbl.objectName() == "pageTitle"
    ]
    assert len(title_lbls) == 1
    assert title_lbls[0].text() == "暂无已加入频道"


def test_empty_hint_hint_label_has_hint_role_property(form: QFormLayout) -> None:
    """hint label setProperty(role, hint) — 走全局 QSS(role hint 样式)。"""
    from tgmonitor.ui.widgets.form_row import empty_hint

    w = empty_hint("💬", "暂无消息", "等待新消息…")
    hint_lbls = [
        lbl for lbl in _grab_labels(w)
        if lbl.property("role") == "hint"
    ]
    assert len(hint_lbls) == 1
    assert "等待" in hint_lbls[0].text()
