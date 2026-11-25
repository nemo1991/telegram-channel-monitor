"""2026-09-09 v1.7.2:BatchProgressDialog 单元测试。

覆盖:
- 构造期默认 op=""/ 标题=「批量操作中…」,非模态
- set_op_name() 运行期改 op + 标题
- _on_progress 推 total 切确定模式 + 更新 status 标签文本
- _on_progress(processed>total)(不应发生,但容错)
- _on_done 成功全过 / 部分失败 / error 三种文案
- closeEvent 解除 signal 连接(关窗后重发不会再触发 _on_progress)
- done() 路径也清理 signal

模式:mock VM(magicmock with batch_progress/batch_done as Qt-style Signal),
直接 .emit 触发 dialog slot。
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from tgmonitor.ui.widgets.batch_progress_dialog import BatchProgressDialog

# ============== helpers ==============


class _MockVM(QObject):
    """最小可用 VM — 只暴露 batch_progress / batch_done Signal 即可。"""

    batch_progress = Signal(object)
    batch_done = Signal(object)


@pytest.fixture
def vm(qapp: QApplication) -> _MockVM:
    return _MockVM()


def _drain(qapp: QApplication) -> None:
    qapp.processEvents()


# ============== 构造 ==============


# ============== 进度更新 ==============


def test_on_done_partial_failure_shows_failed_count(vm: _MockVM, qapp: QApplication) -> None:
    """BatchDone(succeeded=3, failed=2) → status「完成 3 条,失败 2 条」。"""
    dlg = BatchProgressDialog(vm, op="forward", parent=None)
    vm.batch_done.emit(type("E", (), {"succeeded": 3, "failed": 2, "error": None})())
    _drain(qapp)
    assert "3" in dlg.lbl_status.text()
    assert "2" in dlg.lbl_status.text()
    assert "失败" in dlg.lbl_status.text()


# ============== signal 清理 ==============


# ============================================================
# 2026-09-10 v1.7.3:取消按钮 + react emoji 标题
# ============================================================


class _MockVMWithCancel(_MockVM):
    """2026-09-10 v1.7.3:带 cancel_current_batch() 的 VM mock,记录是否被调。"""

    def __init__(self) -> None:
        super().__init__()
        self.cancel_called: int = 0

    def cancel_current_batch(self) -> None:
        self.cancel_called += 1


def test_cancel_button_disables_after_click(qapp: QApplication) -> None:
    """v1.7.3:点取消后按钮 disable — 防双击。"""
    vm = _MockVMWithCancel()
    dlg = BatchProgressDialog(vm, op="delete", parent=None)
    dlg._btn_cancel.click()  # type: ignore[attr-defined]
    _drain(qapp)
    assert not dlg._btn_cancel.isEnabled()  # type: ignore[attr-defined]
    # 第 2 次 click 应被 ignore(disabled),cancel_called 仍 1
    dlg._btn_cancel.click()  # type: ignore[attr-defined]
    _drain(qapp)
    assert vm.cancel_called == 1


# ============================================================
# 2026-09-11 v1.7.4:Cancel ETA / 速率显示 — `BatchProgress` 加
# `elapsed_seconds` / `rate_per_second`,dialog `_on_progress` 算 ETA
# ============================================================


# ============================================================
# 2026-09-11 v1.7.4 regression:disconnect 第二次不抛 RuntimeWarning
# ============================================================


# ============== 2026-09-14 v1.7.5 PR #6 (P0-J):失败明细 ==============
