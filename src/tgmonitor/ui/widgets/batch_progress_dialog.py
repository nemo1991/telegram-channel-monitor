# mypy: disable-error-code="attr-defined"
"""BatchProgressDialog — 批量操作进度对话框 — 2026-09-09 v1.7.2。

设计:
- 非模态(QDialog.setModal(False)),允许批量操作时操作其它面板
- 订阅 `vm.batch_progress` / `vm.batch_done` signal 更新 QProgressBar
- 2026-09-10 v1.7.3:加「取消」按钮 → 调 `vm.cancel_current_batch()` →
  `AppService._cancel_event.set()`,批量 facade 下一个 item break。
  best-effort:已发 TDLib RPC 可能 server-side 仍完成,但 UI 显示
  「操作中断:cancelled」+ 自动 close,符合直觉。
- react / unreact 标题插入 emoji(`"批量回应 😀 中…"`)— `extra` kwarg 传入。
- 完成(BatchDone) → 自动 close;close 后解除 signal 连接。
- 通用 op 字符串("delete"/"mark_read"/"forward"/"pin"/"react" 等),
  标题随 op 切换。
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

# op 字符串 → 用户可见标题的映射(主窗口可显式调 set_op_name 覆盖)
_OP_DEFAULT_TITLES: dict[str, str] = {
    "delete": "批量删除中…",
    "mark_read": "批量标记已读中…",
    "forward": "批量转发中…",
    "pin": "批量钉选中…",
    "unpin": "批量取消钉选中…",
    "react": "批量回应中…",
    "unreact": "批量取消回应中…",
}


class BatchProgressDialog(QDialog):
    """批量操作进度对话框 — 显示当前 BatchProgress + 完成时自动关闭。

    使用模式(MainWindow._on_live_*):
        dlg = BatchProgressDialog(self._vm, op="delete", parent=self)
        dlg.show()
        await self._vm.app.delete_messages_batch(items)
        # BatchDone 自动 emit → dialog 收尾

    2026-09-10 v1.7.3:加 `extra` kwarg 用于 react/unreact 标题显示 emoji;
    加「取消」按钮触发协作式 cancel。
    """

    # 操作完成(成功 / 失败 / 取消)统一发,UI statusbar 据此显示"已完成 N 条"。
    finished = Signal(object)  # payload BatchDone

    def __init__(self, vm, op: str = "", extra: str = "", parent=None) -> None:
        """`op` 为空用「批量操作中…」通用标题;非空走 `_OP_DEFAULT_TITLES`。

        2026-09-10 v1.7.3:`extra` 在 react/unreact 时传入 emoji char,标题
        会插入(`"批量回应 😀 中…"`);其他 op 忽略 `extra`。
        """
        super().__init__(parent)
        self._vm = vm
        self._op = op
        self._extra = extra
        title = _OP_DEFAULT_TITLES.get(op, "批量操作中…")
        if op in ("react", "unreact") and extra:
            # 「批量回应 😀 中…」 — 把 emoji 插到「中」前
            title = title.replace("中…", f"{extra} 中…")
        self.setWindowTitle(self.tr(title))
        self.setModal(False)
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        self.lbl_status = QLabel(self.tr("准备中…"))
        root.addWidget(self.lbl_status)
        self.bar = QProgressBar()
        self.bar.setMinimum(0)
        self.bar.setMaximum(0)  # 默认 indeterminate,直到第一个 BatchProgress 推 total
        root.addWidget(self.bar)
        # 2026-09-10 v1.7.3:取消按钮行
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._btn_cancel = QPushButton(self.tr("取消"))
        self._btn_cancel.setObjectName("batchProgressCancelBtn")
        self._btn_cancel.clicked.connect(self._on_cancel_clicked)
        btn_row.addWidget(self._btn_cancel)
        root.addLayout(btn_row)
        # 订阅 VM progress / done signal
        self._vm.batch_progress.connect(self._on_progress)
        self._vm.batch_done.connect(self._on_done)

    def _on_cancel_clicked(self) -> None:
        """2026-09-10 v1.7.3:取消按钮 — 调 `vm.cancel_current_batch()`。

        best-effort:已发 RPC 可能已完成。disable 自身防双击;真正收尾
        等 BatchDone 到达(由 _on_done 处理)。
        """
        self.lbl_status.setText(self.tr("正在取消…"))
        self._btn_cancel.setEnabled(False)
        if hasattr(self._vm, "cancel_current_batch"):
            self._vm.cancel_current_batch()

    def set_op_name(self, op: str) -> None:
        """运行期改 op(同 dialog 复用多种批量动作时,主窗口可调)。"""
        self._op = op
        title = _OP_DEFAULT_TITLES.get(op, "批量操作中…")
        if op in ("react", "unreact") and self._extra:
            title = title.replace("中…", f"{self._extra} 中…")
        self.setWindowTitle(self.tr(title))

    def _on_progress(self, e) -> None:
        """VM 转发的 BatchProgress 事件。

        `e.total=0` → indeterminate(批开始时 first emit)
        `e.total>0` → 确定模式 + 设值
        """
        total = getattr(e, "total", 0)
        processed = getattr(e, "processed", 0)
        if total <= 0:
            self.bar.setMaximum(0)
            self.lbl_status.setText(self.tr("处理中…"))
        else:
            self.bar.setMaximum(total)
            self.bar.setValue(processed)
            self.lbl_status.setText(
                self.tr("已完成 {done} / {total}").format(done=processed, total=total)
            )

    def _on_done(self, e) -> None:
        """VM 转发的 BatchDone 事件 — 显示完成状态 + 自动 accept。"""
        succeeded = getattr(e, "succeeded", 0)
        failed = getattr(e, "failed", 0)
        error = getattr(e, "error", None)
        if error:
            self.lbl_status.setText(self.tr("操作中断:{err}").format(err=error))
        elif failed > 0:
            self.lbl_status.setText(
                self.tr("完成 {ok} 条,失败 {fail} 条").format(ok=succeeded, fail=failed)
            )
        else:
            self.lbl_status.setText(self.tr("完成 {ok} 条").format(ok=succeeded))
        self.finished.emit(e)
        self.accept()

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt override
        """关窗(X / Alt+F4)清理 signal 连接。

        注:`close()` 默认最终会调 `done()`,避免重复 disconnect,
        这里调 `_disconnect_signals()` 单点处理。
        """
        self._disconnect_signals()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        """QDialog 关闭路径(accept/reject)清理。"""
        self._disconnect_signals()
        super().done(result)

    def _disconnect_signals(self) -> None:
        """单点解连 batch_progress / batch_done — 防 leak。

        try/except 容错:Qt signal disconnect 对已断开的会抛 RuntimeError,
        closeEvent + done() 都会被调到,只解一次也安全。
        """
        for sig, slot in (
            (self._vm.batch_progress, self._on_progress),
            (self._vm.batch_done, self._on_done),
        ):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
