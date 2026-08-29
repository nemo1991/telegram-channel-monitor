"""PR #A3 — ExportProgressDialog 行为测试(信号连接 / 断开 / 进度显示)。"""

from __future__ import annotations

import os

# offscreen:跑测试不弹真窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.core.events import ExportProgress  # noqa: E402
from tgmonitor.ui.widgets.export_progress_dialog import ExportProgressDialog  # noqa: E402


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    """构造一次 QApplication(模块级)— 多次跑 UI 测试不重复创建。"""
    app = QApplication.instance() or QApplication([])
    return app  # type: ignore[return-value]


@pytest.fixture
def vm() -> MagicMock:
    """构造 mock VM — export_progress signal 用 MagicMock 模拟。

    Mock 的 connect/disconnect 不真正生效,但 dialog 代码只调
    connect/disconnect — 不会触发 callback,所以 mock 足够。
    """
    return MagicMock()


def test_dialog_connects_to_vm_on_init(qt_app: QApplication, vm: MagicMock) -> None:
    """构造时必须 connect vm.export_progress — 否则 UI 永远收不到进度。"""
    ExportProgressDialog(vm)
    vm.export_progress.connect.assert_called_once()


def test_dialog_close_disconnects_signal(qt_app: QApplication, vm: MagicMock) -> None:
    """用户拖标题栏关闭(X / Alt+F4)→ disconnect signal — 避免 dangling 引用。"""
    dlg = ExportProgressDialog(vm)
    dlg.close()
    # closeEvent 已调 disconnect — vm 上 disconnect 至少被调一次
    vm.export_progress.disconnect.assert_called()


def test_dialog_done_disconnects_signal(qt_app: QApplication, vm: MagicMock) -> None:
    """`dlg.accept()` 路径(dialog.done 回调)也必须 disconnect — 防双订阅。"""
    dlg = ExportProgressDialog(vm)
    dlg.done(0)  # QDialog.Accepted = 0
    vm.export_progress.disconnect.assert_called()


def test_dialog_progress_handler_accepts_total_none(qt_app: QApplication, vm: MagicMock) -> None:
    """ExportProgress.total=None → QProgressBar 走 indeterminate(max=0)。"""
    dlg = ExportProgressDialog(vm)
    e = ExportProgress(request_id="r1", written=500, total=None)
    dlg._on_progress(e)

    assert dlg.bar.maximum() == 0  # indeterminate
    assert "500" in dlg.lbl_status.text()


def test_dialog_progress_handler_accepts_total_int(qt_app: QApplication, vm: MagicMock) -> None:
    """ExportProgress.total=int → QProgressBar 走确定模式 + 设值 + 状态文字。"""
    dlg = ExportProgressDialog(vm)
    e = ExportProgress(request_id="r1", written=300, total=1000)
    dlg._on_progress(e)

    assert dlg.bar.maximum() == 1000
    assert dlg.bar.value() == 300
    assert "300" in dlg.lbl_status.text()
    assert "1000" in dlg.lbl_status.text()


def test_dialog_cancel_invokes_vm_cancel(qt_app: QApplication, vm: MagicMock) -> None:
    """取消按钮 → vm.cancel_current_export() — UI 不直 cancel task。"""
    dlg = ExportProgressDialog(vm)
    dlg._on_cancel()

    vm.cancel_current_export.assert_called_once()
    # 取消后按钮禁用,避免重复点
    assert dlg.btn_cancel.isEnabled() is False


def test_dialog_cancel_emits_signal(qt_app: QApplication, vm: MagicMock) -> None:
    """取消按钮 → cancelled signal emit(供 main_window 做 statusbar 提示)。"""
    dlg = ExportProgressDialog(vm)
    captured: list[object] = []
    dlg.cancelled.connect(lambda: captured.append(object()))

    dlg._on_cancel()

    assert len(captured) == 1
