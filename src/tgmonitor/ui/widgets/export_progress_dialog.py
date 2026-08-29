# mypy: disable-error-code="attr-defined"
"""ExportProgressDialog — 导出进度对话框(QProgressBar + 取消)— 2026-08-30 v1.5.0 PR #A3。

设计:
- 非模态(QDialog.setModal(False)),允许用户在导出时操作其它面板
- 订阅 `vm.export_progress` signal 更新 QProgressBar:`total=None` 时
  QProgressBar 走 indeterminate mode(流式分页中,总数未知)
- 「取消」按钮 → `vm.cancel_current_export()` — UI 不直接 cancel task,
  走 VM 统一出口,后续可加多导出并发管理时扩展
- 取消 / 完成 / 失败都自动 close;关闭后解除 signal 连接(避免 dangling
  引用)
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)


class ExportProgressDialog(QDialog):
    """导出进度对话框 — 显示当前 ExportProgress + 取消按钮。

    使用模式(MainWindow._on_export):
        dlg = ExportProgressDialog(self._vm, parent=self)
        dlg.show()           # 非模态
        self._vm.start_export(req)
    """

    # 用户取消(在内部已调 vm.cancel_current_export,信号转发出去
    # 给 main_window 做 statusbar 提示)。
    cancelled = Signal()

    def __init__(self, vm, parent=None) -> None:
        """订阅 vm.export_progress,绑取消按钮。

        `vm` 类型故意不 import(MonitorViewModel),避免循环 import —
        vm 只用 3 个属性:`export_progress` signal / `cancel_current_export`
        方法 / `app.bus` 不需要(dialog 不直订阅 EventBus,走 VM signal)。
        """
        super().__init__(parent)
        self._vm = vm
        self.setWindowTitle("导出中")
        # 非模态:用户导出时可继续操作其它面板(导出是 fire-and-forget)
        self.setModal(False)
        # 关窗口 = 取消(用户拖标题栏关闭按钮)
        # 2026-08-30 v1.5.0 PR #A3:QDialog 默认 closeEvent → accept 不调
        # cancel_current_export,我们 override 走 reject 路径
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        # 状态行(初始「导出中…」,完成后「完成」/ 失败显示 error)
        self.lbl_status = QLabel("导出中…")
        root.addWidget(self.lbl_status)
        # 进度条
        self.bar = QProgressBar()
        self.bar.setMinimum(0)
        self.bar.setMaximum(0)  # 0 = indeterminate,默认
        root.addWidget(self.bar)
        # 取消按钮
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self._on_cancel)
        row.addWidget(self.btn_cancel)
        root.addLayout(row)
        # 订阅 VM progress signal
        self._vm.export_progress.connect(self._on_progress)

    def _on_progress(self, e) -> None:
        """VM 转发的 ExportProgress 事件。

        `e.total=None` → indeterminate 模式(spinner)
        `e.total=int` → 确定模式 + 设值 + 写状态文字
        """
        total = getattr(e, "total", None)
        written = getattr(e, "written", 0)
        if total is None:
            # 流式分页中 — indeterminate spinner,文字显示已写条数
            self.bar.setMaximum(0)
            self.lbl_status.setText(f"导出中…已写 {written} 条")
        else:
            self.bar.setMaximum(total)
            self.bar.setValue(written)
            self.lbl_status.setText(f"已完成 {written} / {total}")

    def _on_cancel(self) -> None:
        """取消按钮 → VM 取消当前 export。

        不直接关 dialog:让 export 真正结束(ExportDone event → vm._export_task
        清空)后由 `_on_export_done` 触发关闭,避免半截写入假完成 UI。
        """
        self.btn_cancel.setEnabled(False)
        self.lbl_status.setText("正在取消…")
        self._vm.cancel_current_export()
        self.cancelled.emit()

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt override
        """2026-08-30 v1.5.0 PR #A3:窗口被关(X / Alt+F4)等同取消。

        解除 signal 连接,避免 dangling pointer;不主动 cancel — 用户可能
        想让导出在后台跑完,关窗只是不显示进度条。
        """
        try:
            self._vm.export_progress.disconnect(self._on_progress)
        except (RuntimeError, TypeError):
            # 已 disconnect(Qt 抛 RuntimeError);TypeError 是 slot 已 GC
            pass
        super().closeEvent(event)

    def done(self, result: int) -> None:
        """2026-08-30 v1.5.0 PR #A3:QDialog 关闭路径(accept/reject)清理。

        `ExportDone` 事件触发 vm._on_export_done 自动调 `dlg.accept()` —
        此方法负责断开 signal + 走父类 done。
        """
        try:
            self._vm.export_progress.disconnect(self._on_progress)
        except (RuntimeError, TypeError):
            pass
        super().done(result)
