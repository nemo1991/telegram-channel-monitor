# mypy: disable-error-code=attr-defined
# 访问 `MainWindow._export_dialog` — controller / MainWindow thin delegate
# 共享同一字段(export 期间有值,完成后 del)。
"""导出 controller — `_on_export` / `_on_export_progress` / `_on_export_done`。

2026-10-01 v1.12.x 从 `MainWindow` 抽出。3 个 handler:
- `_on_export`:Dashboard 顶部「导出」按钮 → 弹 ExportDialog + ExportProgressDialog
- `_on_export_progress`:VM `export_progress` signal → 状态栏活动指示器
- `_on_export_done`:VM `export_done` signal → 关进度对话框 + 状态栏提示

`_export_dialog` 字段(进度对话框实例)挂 MainWindow 上,export 期间有值,
完成后 `del` — 保留这个 idiom(controller 通过 ctx.main_window 访问)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tgmonitor.ui.controllers._base import MainWindowCtx

if TYPE_CHECKING:
    pass


class ExportController:
    """导出入口 + 进度反馈。"""

    def __init__(self, ctx: MainWindowCtx) -> None:
        self._ctx = ctx

    def on_export(self) -> None:
        mw = self._ctx.main_window
        if not self._ctx.monitor.subscribed_ids:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(
                mw,
                mw.tr("导出"),
                mw.tr("请先订阅至少一个频道"),
            )
            return
        ids = sorted(int(cid) for cid in self._ctx.monitor.subscribed_ids)
        from tgmonitor.ui.widgets.export_dialog import ExportDialog
        from tgmonitor.ui.widgets.export_progress_dialog import ExportProgressDialog

        dlg = ExportDialog(self._ctx.app, ids, mw)
        if dlg.exec():
            req = dlg.request()
            # 2026-08-30 v1.5.0 PR #A3:导出参数敲定后弹进度对话框 +
            # 后台 start_export。dialog 自身订阅 vm.export_progress +
            # 完成后由 _on_export_done 关闭。
            self._ctx.main_window._export_dialog = ExportProgressDialog(self._ctx.vm, parent=mw)
            self._ctx.main_window._export_dialog.show()
            self._ctx.vm.start_export(req)

    def on_export_progress(self, progress: object) -> None:
        """导出进度 → 左侧活动指示器持续显示。dialog 自身的进度条同步显示。"""
        # progress 可能是 dict 或 dataclass;兼容两种
        done = getattr(progress, "done", None)
        total = getattr(progress, "total", None)
        if done is None and isinstance(progress, dict):
            done = progress.get("done")
            total = progress.get("total")
        if done is not None and total:
            self._ctx.status_bar.show_activity(f"导出 {done}/{total}")

    def on_export_done(self, result: dict | None, error: str | None) -> None:
        mw = self._ctx.main_window
        # 2026-08-30 v1.5.0 PR #A3:关闭进度对话框(如有)— dialog 自身
        # 已解 signal 连接,accept() 安全
        dlg = getattr(mw, "_export_dialog", None)
        if dlg is not None:
            dlg.accept()
            # del 而非 = None:避免 ExportProgressDialog | None 注解变化
            # 蔓延全文件;此字段本来就只在 export 期间有值
            del mw._export_dialog
        if error:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(mw, mw.tr("导出失败"), error)
            self._ctx.status_bar.show_activity(f"⚠ 导出失败: {error}", timeout_ms=5000)
        elif result:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(
                mw,
                mw.tr("导出完成"),
                mw.tr("已写入 {path}\n{n_msg} 条消息,{n_bytes} 字节").format(
                    path=result["out_path"],
                    n_msg=result["message_count"],
                    n_bytes=result["bytes_written"],
                ),
            )
            self._ctx.status_bar.show_activity(f"导出完成: {result['out_path']}", timeout_ms=4000)
