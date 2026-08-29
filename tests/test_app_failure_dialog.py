"""启动失败 GUI 弹窗单元测试 — app.py `_show_setup_failure_dialog`。

mock QApplication + QMessageBox.exec 验证弹窗不抛、不污染 qt_app 退出。

QT_QPA_PLATFORM=offscreen 无 GUI,但 `QApplication.instance()` 仍可解析;
`QMessageBox.exec` 我们通过 monkeypatch 替换,避免真弹 modal 阻塞测试。
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from tgmonitor.app import _show_setup_failure_dialog


@pytest.fixture
def qt_app() -> QApplication:
    """Ensure QApplication exists (offscreen)。"""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def test_dialog_runs_and_does_not_raise(
    qt_app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最简版本:mock QMessageBox.exec 替代真弹窗,验证 _show_setup_failure_dialog
    走完不抛。"""
    captured: dict = {}

    def fake_exec(self: QMessageBox) -> int:
        # 在 offscreen 模式 `windowTitle()` 返空(平台插件不显示 window),
        # 改用 `text()` 抓 dialog 文本断言
        captured["text"] = self.text()
        captured["detailed"] = self.detailedText()
        captured["icon"] = self.icon()
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)

    err = RuntimeError("API_ID not set")
    _show_setup_failure_dialog(err)

    assert "text" in captured
    assert "API_ID not set" in captured["text"]
    # detailed text 应该是 platform-native log 路径
    assert captured["detailed"] != ""
    # icon 是 Critical
    assert captured["icon"] == QMessageBox.Icon.Critical


def test_dialog_handles_exception_with_empty_message(
    qt_app: QApplication,
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


def test_dialog_silent_when_qt_app_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QApplication.instance() 返 None 时,弹窗静默退化(不抛,只 log)。"""
    # Monkeypatch `QApplication.instance` 返 None
    monkeypatch.setattr(QApplication, "instance", classmethod(lambda cls: None))
    # 不应有 QMessageBox.exec 调用
    exec_called: list[bool] = []

    def fail_exec(self: QMessageBox) -> int:
        exec_called.append(True)
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "exec", fail_exec)

    # 不 import QApplication,单纯 call,应该走 early return
    _show_setup_failure_dialog(RuntimeError("boom"))
    assert exec_called == []


def test_dialog_swallows_qt_import_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PySide6.QtWidgets import 抛 ImportError 时,弹窗静默退化。"""
    # Patch the import inside _show_setup_failure_dialog
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "PySide6.QtWidgets":
            raise ImportError("simulated PySide6 failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    # Should not raise
    _show_setup_failure_dialog(RuntimeError("boom"))
