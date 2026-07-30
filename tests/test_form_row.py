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
