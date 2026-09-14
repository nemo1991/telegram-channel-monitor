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

import warnings

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
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

        2026-09-14 v1.7.5 PR #6 (P0-J):`_failures` 缓存 BatchDone.failures
        列表,失败 > 0 时显示「查看失败详情」按钮 + 自动弹 dialog。
        """
        super().__init__(parent)
        self._vm = vm
        self._op = op
        self._extra = extra
        self._failures: list[tuple[int, int, str]] = []
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
        # 按钮行 — 取消 + 查看失败详情(失败时启用)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        # 2026-09-14 v1.7.5 PR #6 (P0-J):失败详情按钮 — 默认隐藏,失败>0 时显示
        self._btn_detail = QPushButton(self.tr("查看失败详情"))
        self._btn_detail.setObjectName("batchProgressDetailBtn")
        self._btn_detail.setVisible(False)
        self._btn_detail.clicked.connect(self._on_detail_clicked)
        btn_row.addWidget(self._btn_detail)
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

        2026-09-11 v1.7.4:rate > 0 时附加「N 条/秒,剩余 M 秒」ETA —
        `e.rate_per_second` 来自 facade(`_compute_rate` helper,
        `processed / elapsed_since_facade_start`)。
        """
        total = getattr(e, "total", 0)
        processed = getattr(e, "processed", 0)
        rate = getattr(e, "rate_per_second", 0.0)
        if total <= 0:
            self.bar.setMaximum(0)
            self.lbl_status.setText(self.tr("处理中…"))
            return
        self.bar.setMaximum(total)
        self.bar.setValue(processed)
        if rate > 0 and processed < total:
            eta_seconds = (total - processed) / rate
            self.lbl_status.setText(
                self.tr("已完成 {done} / {total} — {rate:.1f} 条/秒,剩余 {eta:.0f} 秒").format(
                    done=processed, total=total, rate=rate, eta=eta_seconds
                )
            )
        else:
            self.lbl_status.setText(
                self.tr("已完成 {done} / {total}").format(done=processed, total=total)
            )

    def _on_done(self, e) -> None:
        """VM 转发的 BatchDone 事件 — 显示完成状态 + 自动 accept。

        2026-09-14 v1.7.5 PR #6 (P0-J):失败 > 0 时显示「查看失败详情」按钮,
        点击弹出 `BatchFailureDetailDialog` 展示每条 (cid, mid, error_str)。
        """
        succeeded = getattr(e, "succeeded", 0)
        failed = getattr(e, "failed", 0)
        error = getattr(e, "error", None)
        failures = getattr(e, "failures", []) or []
        if error:
            self.lbl_status.setText(self.tr("操作中断:{err}").format(err=error))
        elif failed > 0:
            self.lbl_status.setText(
                self.tr("完成 {ok} 条,失败 {fail} 条").format(ok=succeeded, fail=failed)
            )
        else:
            self.lbl_status.setText(self.tr("完成 {ok} 条").format(ok=succeeded))
        # 失败 > 0 且有具体 failures 列表 → 显示详情按钮
        if failed > 0 and failures:
            self._failures = failures
            self._btn_detail.setVisible(True)
            # 自动弹一次(让用户立刻看到);非模态,不阻塞主窗口
            self._show_failure_dialog()
        self.finished.emit(e)
        self.accept()

    def _on_detail_clicked(self) -> None:
        """2026-09-14 v1.7.5 PR #6 (P0-J):手动重开失败详情 dialog。"""
        self._show_failure_dialog()

    def _show_failure_dialog(self) -> None:
        """弹 `BatchFailureDetailDialog` 展示每条失败 (cid, mid, error_str)。

        用 `.show()`(非模态)而非 `.exec()`,这样:
          - 不阻塞 BatchProgressDialog 关闭路径(`self.accept()` 后续)
          - 用户可在主窗口操作的同时查看详情
          - 父对象=BatchProgressDialog,关窗时 Qt 自动清理
        """
        dlg = BatchFailureDetailDialog(self._failures, parent=self)
        dlg.show()

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
        注:PySide6 第二次 disconnect 走 shiboken 路径会打 RuntimeWarning
        而非 raise,关窗后 emit 测试的 stderr 噪声源 — 用 catch_warnings
        吞掉 RuntimeWarning,行为不变。
        """
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            for sig, slot in (
                (self._vm.batch_progress, self._on_progress),
                (self._vm.batch_done, self._on_done),
            ):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass


class BatchFailureDetailDialog(QDialog):
    """2026-09-14 v1.7.5 PR #6 (P0-J):批量失败明细对话框。

    列出每条失败的 `(cid, mid, error_str)` — 用户可滚动 / 复制到剪贴板。
    模态(`exec()`),但 parent 是 `BatchProgressDialog`,故主窗口不阻塞。
    """

    def __init__(self, failures: list[tuple[int, int, str]], parent=None) -> None:
        super().__init__(parent)
        self._failures = list(failures)
        self.setObjectName("batchFailureDetailDialog")
        self.setWindowTitle(self.tr("批量操作失败详情"))
        self.resize(640, 360)
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        # 顶部摘要
        header = QLabel(self.tr("共 {n} 条失败:").format(n=len(self._failures)))
        header.setObjectName("batchFailureHeader")
        root.addWidget(header)
        # 失败列表
        self.list = QListWidget()
        for cid, mid, err in self._failures:
            item = QListWidgetItem(self._format_row(cid, mid, err))
            self.list.addItem(item)
        root.addWidget(self.list, 1)
        # 按钮行 — 关闭
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_close = QPushButton(self.tr("关闭"))
        btn_close.setObjectName("batchFailureCloseBtn")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

    def _format_row(self, cid: int, mid: int, err: str) -> str:
        # 三列对齐:cid / mid / error — 等宽风格便于扫读
        return f"cid={cid:<12}  mid={mid:<12}  {err}"
