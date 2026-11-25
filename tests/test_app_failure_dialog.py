"""启动失败 GUI 弹窗单元测试 — app.py `_show_setup_failure_dialog`。

mock QApplication + QMessageBox.exec 验证弹窗不抛、不污染 qt_app 退出。

QT_QPA_PLATFORM=offscreen 无 GUI,但 `QApplication.instance()` 仍可解析;
`QMessageBox.exec` 我们通过 monkeypatch 替换,避免真弹 modal 阻塞测试。
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from tgmonitor.app import _show_setup_failure_dialog


def test_dialog_handles_exception_with_empty_message(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常 str() 空时,显示 '(no message)' 而不是空字符串。"""
    captured: list[str] = []

    def fake_exec(self: QMessageBox) -> int:
        captured.append(self.text())
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)

    class _EmptyMsg(Exception):
        def __str__(self) -> str:
            return ""

    _show_setup_failure_dialog(_EmptyMsg())
    assert len(captured) == 1
    assert "(no message)" in captured[0]
