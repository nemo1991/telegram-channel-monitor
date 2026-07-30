"""form_row.py — settings_page / export_dialog 用的 form row 工厂。

集中 QFormLayout 的两种高频样板:
  1. text_field(form, label, placeholder)        → QLineEdit + addRow
  2. path_field(form, label, placeholder, *,
                on_default=..., parent=...)       → QLineEdit + 浏览 +
                                                    (可选)默认 + addRow

设计上保留 QFormLayout 的 row index 语义 — caller 仍调 `form.addRow(...)`,
helper 只是"创建 widget 并把它绑进 form row",所以 `_find_form_row`
(QFormLayout row index 寻址,见 `settings_page._find_form_row`)继续工作。

不抽 `addRow` 本身(3 行 boilerplate 抽起来反而绕),只抽"建 widget 并把它
嵌进 row"成本。

`path_field` 把 QHBoxLayout 套进一个 inner `QWidget`(因为 QFormLayout.FieldRole
推荐传 QWidget,直接传 QHBoxLayout 会有 layout 警告)。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)


def text_field(
    form: QFormLayout,
    label: str,
    placeholder: str = "",
    *,
    echo_password: bool = False,
) -> QLineEdit:
    """Simple text field — addRow + return QLineEdit.

    Caller 可继续 setValidator / setText / setMaxLength 等;如果需要
    浏览 / 默认按钮,用 `path_field`。
    """
    edit = QLineEdit()
    edit.setPlaceholderText(placeholder)
    if echo_password:
        edit.setEchoMode(QLineEdit.EchoMode.Password)
    form.addRow(label, edit)
    return edit


def combo_field(
    form: QFormLayout,
    label: str,
    options: Iterable[tuple[Any, str]] | Iterable[Any],
) -> QComboBox:
    """ComboBox + addItem(枚举/对) + addRow,返 QComboBox。

    参数:
      - `options`:可迭代;支持两种形态:
        - `Iterable[Enum]` — 枚举类,自动用 `opt.value` 当显示文本
          (DBBackend / ObjectStoreBackend / MediaPolicy 等)
        - `Iterable[tuple[Any, str]]` — (data, display_text) 对(给自定义选项)
      addRow(label, cmb) 由 helper 调;caller 之后仍可 `cmb.currentIndexChanged.connect(...)`
      / `cmb.findData(...)` / `cmb.currentData()` 等。

    例子:
        combo_field(form, "后端:", DBBackend)        # 枚举
        combo_field(form, "性别:", [("m", "男"), ("f", "女")])  # 对
    """
    cmb = QComboBox()
    for opt in options:
        if isinstance(opt, tuple):
            data, display_text = opt
        else:
            data = opt
            display_text = opt.value
        cmb.addItem(display_text, data)
    form.addRow(label, cmb)
    return cmb


def spin_field(
    form: QFormLayout,
    label: str,
    *,
    min: int = 0,
    max: int = 100,
    value: int = 0,
    suffix: str = "",
    single_step: int = 1,
    tooltip: str = "",
) -> QSpinBox:
    """SpinBox + setRange/setValue/setSuffix/setSingleStep/setToolTip + addRow,
    返 QSpinBox。

    参数全是 keyword-only,避免与 `value` / `min` / `max` Python builtin 关键字
    风格混淆。caller 之后仍可 `spin.setValue(...)` / `spin.value()` 等。

    例子:
        spin_field(form, "API ID:", min=0, max=2_000_000_000, value=0)
        spin_field(form, "单文件大小上限:", min=0, max=10240,
                   suffix=" MB", single_step=10,
                   tooltip="0 = 无限制")
    """
    spin = QSpinBox()
    spin.setRange(min, max)
    spin.setValue(value)
    if suffix:
        spin.setSuffix(suffix)
    spin.setSingleStep(single_step)
    if tooltip:
        spin.setToolTip(tooltip)
    form.addRow(label, spin)
    return spin


def path_field(
    form: QFormLayout,
    label: str,
    placeholder: str = "",
    *,
    on_default: Callable[[], None] | None = None,
    default_tooltip: str = "恢复为 platform-native 默认目录",
    parent: QWidget | None = None,
    file_mode: bool = False,
) -> QLineEdit:
    """Path field with 「浏览…」 + (可选)「默认」按钮 + addRow。

    浏览按钮:
      - `file_mode=False`(默认):打开 `QFileDialog.getExistingDirectory`,
        选目录 — 适用于 settings_page 的 4 个目录字段
      - `file_mode=True`:打开 `QFileDialog.getSaveFileName`,选保存文件路径
        — 适用于 export_dialog 的输出文件

    默认按钮:`on_default` 是无参 callable(典型用法 `lambda: edit.setText(...)`),
    None = 不显示「默认」按钮。

    Returns the QLineEdit so caller 可调 `.text()` / `.setText()` 等。
    """
    edit = QLineEdit()
    edit.setPlaceholderText(placeholder)

    row_w = QWidget()
    row = QHBoxLayout(row_w)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(edit, 1)

    btn_browse = QPushButton("浏览…")
    if file_mode:
        btn_browse.clicked.connect(lambda: _on_browse_file(edit, parent))
    else:
        btn_browse.clicked.connect(lambda: _on_browse_dir(edit, parent))
    row.addWidget(btn_browse)

    if on_default is not None:
        btn_default = QPushButton("默认")
        btn_default.setToolTip(default_tooltip)
        btn_default.clicked.connect(on_default)
        row.addWidget(btn_default)

    form.addRow(label, row_w)
    return edit


def _on_browse_dir(edit: QLineEdit, parent: QWidget | None) -> None:
    """Internal: open directory picker;set QLineEdit text on user confirm."""
    dir_path = QFileDialog.getExistingDirectory(parent, "选择目录", edit.text())
    if dir_path:
        edit.setText(dir_path)


def _on_browse_file(edit: QLineEdit, parent: QWidget | None) -> None:
    """Internal: open save-file picker;set QLineEdit text on user confirm."""
    path, _ = QFileDialog.getSaveFileName(parent, "选择输出文件", edit.text())
    if path:
        edit.setText(path)