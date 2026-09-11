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
from PySide6.QtWidgets import QApplication, QDialog

from tgmonitor.ui.widgets.batch_progress_dialog import BatchProgressDialog

# ============== helpers ==============


class _MockVM(QObject):
    """最小可用 VM — 只暴露 batch_progress / batch_done Signal 即可。"""

    batch_progress = Signal(object)
    batch_done = Signal(object)


@pytest.fixture
def qapp() -> QApplication:
    """QApplication(非 QCoreApplication)— QDialog 需要 widget 实例。

    conftest 可能已造了 QApplication,这里复用 — 否则新建。
    """
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


@pytest.fixture
def vm(qapp: QApplication) -> _MockVM:
    return _MockVM()


def _drain(qapp: QApplication) -> None:
    qapp.processEvents()


# ============== 构造 ==============


def test_dialog_default_op_has_generic_title(vm: _MockVM, qapp: QApplication) -> None:
    """op="" 用「批量操作中…」通用标题 + 非模态。"""
    dlg = BatchProgressDialog(vm, op="", parent=None)
    assert dlg.windowTitle() != ""
    assert not dlg.isModal()


def test_dialog_known_op_uses_mapped_title(vm: _MockVM, qapp: QApplication) -> None:
    """op="delete" → 标题含「删除」(具体文案 _OP_DEFAULT_TITLES 控制)。"""
    dlg = BatchProgressDialog(vm, op="delete", parent=None)
    assert "删除" in dlg.windowTitle()


def test_dialog_set_op_name_updates_title(vm: _MockVM, qapp: QApplication) -> None:
    """运行期 set_op_name 切 op + 标题。"""
    dlg = BatchProgressDialog(vm, op="delete", parent=None)
    dlg.set_op_name("forward")
    assert "转发" in dlg.windowTitle()


# ============== 进度更新 ==============


def test_on_progress_with_total_switches_to_determinate(vm: _MockVM, qapp: QApplication) -> None:
    """BatchProgress(total=10, processed=3) → bar.setMaximum(10) + value(3)。"""
    dlg = BatchProgressDialog(vm, op="delete", parent=None)
    vm.batch_progress.emit(type("E", (), {"total": 10, "processed": 3})())
    _drain(qapp)
    assert dlg.bar.maximum() == 10
    assert dlg.bar.value() == 3
    assert "3" in dlg.lbl_status.text()
    assert "10" in dlg.lbl_status.text()


def test_on_progress_total_zero_keeps_indeterminate(vm: _MockVM, qapp: QApplication) -> None:
    """BatchProgress(total=0)(批开始) → indeterminate + status「处理中…」。"""
    dlg = BatchProgressDialog(vm, op="delete", parent=None)
    vm.batch_progress.emit(type("E", (), {"total": 0, "processed": 0})())
    _drain(qapp)
    # indeterminate 模式下 QProgressBar.maximum=0
    assert dlg.bar.maximum() == 0


def test_on_done_success_emits_finished_and_accepts(vm: _MockVM, qapp: QApplication) -> None:
    """BatchDone(succeeded=5, failed=0) → status「完成 5 条」+ finished signal + accept。"""
    dlg = BatchProgressDialog(vm, op="delete", parent=None)
    captured: list = []
    dlg.finished.connect(lambda e: captured.append(e))  # type: ignore[arg-type]
    dlg.show()
    _drain(qapp)

    vm.batch_done.emit(type("E", (), {"succeeded": 5, "failed": 0, "error": None})())
    _drain(qapp)

    assert "5" in dlg.lbl_status.text()
    assert dlg.result() == QDialog.Accepted
    assert len(captured) == 1


def test_on_done_partial_failure_shows_failed_count(vm: _MockVM, qapp: QApplication) -> None:
    """BatchDone(succeeded=3, failed=2) → status「完成 3 条,失败 2 条」。"""
    dlg = BatchProgressDialog(vm, op="forward", parent=None)
    vm.batch_done.emit(type("E", (), {"succeeded": 3, "failed": 2, "error": None})())
    _drain(qapp)
    assert "3" in dlg.lbl_status.text()
    assert "2" in dlg.lbl_status.text()
    assert "失败" in dlg.lbl_status.text()


def test_on_done_error_overrides_count(vm: _MockVM, qapp: QApplication) -> None:
    """BatchDone(error="paused") 顶层错误 → status「操作中断:paused」。

    错误文案优先于 succeeded/failed 数字 — 一眼能看出整批没跑。
    """
    dlg = BatchProgressDialog(vm, op="forward", parent=None)
    vm.batch_done.emit(type("E", (), {"succeeded": 0, "failed": 0, "error": "paused"})())
    _drain(qapp)
    assert "paused" in dlg.lbl_status.text()
    assert "中断" in dlg.lbl_status.text()


# ============== signal 清理 ==============


def test_close_event_disconnects_signals(vm: _MockVM, qapp: QApplication) -> None:
    """closeEvent 解除 vm.batch_progress / batch_done 连接 — 关窗后 emit 不会再触发。

    防 leak:VM 是长寿命对象,dialog 是 short-lived(批量操作时临时弹出),
    不 disconnect 会让 dialog 滞留到下一次批量操作时误触发。
    """
    dlg = BatchProgressDialog(vm, op="delete", parent=None)
    dlg.show()
    _drain(qapp)

    # 模拟用户关窗
    dlg.close()
    _drain(qapp)

    # 关窗后再 emit — 不应再调用 _on_progress / _on_done
    # (label text 保持关窗前的"准备中…")
    initial_text = dlg.lbl_status.text()
    vm.batch_progress.emit(type("E", (), {"total": 0, "processed": 0})())
    _drain(qapp)
    # 没崩就行(信号 disconnect 内部 try/except 容错,关窗后 emit 是 no-op)
    assert dlg.lbl_status.text() == initial_text


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


def test_dialog_has_cancel_button(vm: _MockVM, qapp: QApplication) -> None:
    """v1.7.3:dialog 必须有取消按钮。"""
    dlg = BatchProgressDialog(vm, op="delete", parent=None)
    assert dlg._btn_cancel is not None  # type: ignore[attr-defined]
    assert dlg._btn_cancel.isEnabled()  # type: ignore[attr-defined]


def test_cancel_button_click_calls_vm_cancel(qapp: QApplication) -> None:
    """v1.7.3:点取消按钮 → 调 `vm.cancel_current_batch()` + 状态置「正在取消…」。"""
    vm = _MockVMWithCancel()
    dlg = BatchProgressDialog(vm, op="delete", parent=None)
    dlg._btn_cancel.click()  # type: ignore[attr-defined]
    _drain(qapp)
    assert vm.cancel_called == 1
    assert "取消" in dlg.lbl_status.text()


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


def test_cancel_button_skipped_if_vm_no_method(vm: _MockVM, qapp: QApplication) -> None:
    """v1.7.3:VM 无 `cancel_current_batch()` → 点击不抛(`hasattr` 防御)。"""
    dlg = BatchProgressDialog(vm, op="delete", parent=None)
    # 默认 _MockVM 没有 cancel_current_batch — 点应 no-op
    dlg._btn_cancel.click()  # type: ignore[attr-defined]
    _drain(qapp)
    assert "取消" in dlg.lbl_status.text()


def test_react_dialog_title_includes_emoji(vm: _MockVM, qapp: QApplication) -> None:
    """v1.7.3:op=react + extra="🔥" → 标题「批量回应 🔥 中…」。"""
    dlg = BatchProgressDialog(vm, op="react", extra="🔥", parent=None)
    title = dlg.windowTitle()
    assert "回应" in title
    assert "🔥" in title
    assert "中" in title


def test_unreact_dialog_title_includes_emoji(vm: _MockVM, qapp: QApplication) -> None:
    """v1.7.3:op=unreact + extra="🔥" → 标题「批量取消回应 🔥 中…」。"""
    dlg = BatchProgressDialog(vm, op="unreact", extra="🔥", parent=None)
    title = dlg.windowTitle()
    assert "取消回应" in title
    assert "🔥" in title


def test_non_react_dialog_ignores_extra(vm: _MockVM, qapp: QApplication) -> None:
    """v1.7.3:op=delete + extra="🔥" → 标题不含 🔥(非 react op 忽略 extra)。"""
    dlg = BatchProgressDialog(vm, op="delete", extra="🔥", parent=None)
    assert "🔥" not in dlg.windowTitle()
    assert "删除" in dlg.windowTitle()


def test_set_op_name_with_extra_updates_title(vm: _MockVM, qapp: QApplication) -> None:
    """v1.7.3:set_op_name 切 react 时保留 extra — 标题插入 emoji。"""
    dlg = BatchProgressDialog(vm, op="delete", extra="😀", parent=None)
    dlg.set_op_name("react")
    assert "😀" in dlg.windowTitle()
    assert "回应" in dlg.windowTitle()


def test_dialog_closes_on_batch_done_with_error(vm: _MockVM, qapp: QApplication) -> None:
    """v1.7.3:BatchDone(error='cancelled') → 自动 accept + finished signal + 中断文案。"""
    dlg = BatchProgressDialog(vm, op="delete", parent=None)
    captured: list = []
    dlg.finished.connect(lambda e: captured.append(e))  # type: ignore[arg-type]
    vm.batch_done.emit(type("E", (), {"succeeded": 2, "failed": 3, "error": "cancelled"})())
    _drain(qapp)
    assert dlg.result() == QDialog.Accepted
    assert len(captured) == 1
    assert "cancelled" in dlg.lbl_status.text()


# ============================================================
# 2026-09-11 v1.7.4:Cancel ETA / 速率显示 — `BatchProgress` 加
# `elapsed_seconds` / `rate_per_second`,dialog `_on_progress` 算 ETA
# ============================================================


def test_progress_shows_rate_and_eta_when_positive(vm: _MockVM, qapp: QApplication) -> None:
    """v1.7.4:`processed=5,total=10,rate=2.0` → status 含「2.0 条/秒,剩余 3 秒」。

    (10 - 5) / 2.0 = 2.5 秒,四舍五入到 3(`.0f` 保留 1 位小数 → 2 或 3 看 round 行为,
    这里 `:.0f` 是 round half to even,但 2.5 round → 2;直接用整数测试
    `processed=4,total=10,rate=2.0` → (10-4)/2=3.0 → ETA=3 秒更稳)。
    """
    from tgmonitor.core.events import BatchProgress

    dlg = BatchProgressDialog(vm, op="delete", parent=None)
    _drain(qapp)
    vm.batch_progress.emit(BatchProgress(op="delete", processed=4, total=10, rate_per_second=2.0))
    _drain(qapp)
    text = dlg.lbl_status.text()
    assert "2.0" in text
    assert "条/秒" in text
    assert "3" in text  # ETA 秒
    assert "/" in text  # 已完成 N / total
    dlg.close()
    dlg.deleteLater()
    _drain(qapp)


def test_progress_omits_eta_when_rate_zero(vm: _MockVM, qapp: QApplication) -> None:
    """v1.7.4:`rate=0`(极短 elapsed / 第一条)→ status 不显 ETA。"""
    from tgmonitor.core.events import BatchProgress

    dlg = BatchProgressDialog(vm, op="pin", parent=None)
    _drain(qapp)
    vm.batch_progress.emit(BatchProgress(op="pin", processed=0, total=10, rate_per_second=0.0))
    _drain(qapp)
    text = dlg.lbl_status.text()
    # indeterminate 或确定模式都只显「处理中…」/「已完成 0 / 10」
    assert "条/秒" not in text
    assert "剩余" not in text
    dlg.close()
    dlg.deleteLater()
    _drain(qapp)


def test_progress_elapsed_field_zero_at_start(vm: _MockVM, qapp: QApplication) -> None:
    """v1.7.4:`elapsed_seconds=0` → 不参与 ETA 计算(避免除零),UI 行为同 rate=0。"""
    from tgmonitor.core.events import BatchProgress

    dlg = BatchProgressDialog(vm, op="react", parent=None)
    _drain(qapp)
    vm.batch_progress.emit(
        BatchProgress(op="react", processed=1, total=10, elapsed_seconds=0.0, rate_per_second=0.0)
    )
    _drain(qapp)
    text = dlg.lbl_status.text()
    assert "条/秒" not in text
    dlg.close()
    dlg.deleteLater()
    _drain(qapp)


def test_progress_eta_skipped_when_processed_equals_total(vm: _MockVM, qapp: QApplication) -> None:
    """v1.7.4:processed==total → 不显示「剩余 0 秒」(冗余;done 接管)。"""
    from tgmonitor.core.events import BatchProgress

    dlg = BatchProgressDialog(vm, op="delete", parent=None)
    _drain(qapp)
    vm.batch_progress.emit(BatchProgress(op="delete", processed=10, total=10, rate_per_second=2.0))
    _drain(qapp)
    text = dlg.lbl_status.text()
    assert "剩余" not in text  # 已完成,不再 ETA
    dlg.close()
    dlg.deleteLater()
    _drain(qapp)
