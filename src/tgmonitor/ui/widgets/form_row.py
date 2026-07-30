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

from collections.abc import Callable

from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
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


def path_field(
    form: QFormLayout,
    label: str,
    placeholder: str = "",
    *,
    on_default: Callable[[], None] | None = None,
    default_tooltip: str = "恢复为 platform-native 默认目录",
    parent: QWidget | None = None,
) -> QLineEdit:
    """Path field with 「浏览…」 + (可选)「默认」按钮 + addRow。

    浏览按钮:打开 `QFileDialog.getExistingDirectory`,选完写回 QLineEdit。
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
    btn_browse.clicked.connect(lambda: _on_browse(edit, parent))
    row.addWidget(btn_browse)

    if on_default is not None:
        btn_default = QPushButton("默认")
        btn_default.setToolTip(default_tooltip)
        btn_default.clicked.connect(on_default)
        row.addWidget(btn_default)

    form.addRow(label, row_w)
    return edit


def _on_browse(edit: QLineEdit, parent: QWidget | None) -> None:
    """Internal: open directory picker;set QLineEdit text on user confirm."""
    dir_path = QFileDialog.getExistingDirectory(parent, "选择目录", edit.text())
    if dir_path:
        edit.setText(dir_path)