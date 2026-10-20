# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""ActionsMixin — 选中集 / 批量操作 / 导出 — 2026-10-20 plan-batch10-γ。

`MediaManagerWidget` 拆 3 mixin + facade 的**第三步**:抽交互动作域。

- 11 method:`_selected_keys` / `_on_selection_changed` / `_find_row_by_key`
  / `_on_select_all` / `_on_batch_retry` / `_on_batch_delete`
  / `_on_clear_channel` / `_on_export_csv` / `_on_export_zip`
  / `on_channel_cleared` / `_row_is_failed`

## i18n:5 处 tr 字面量为什么不跟过来

本 mixin 原本含 5 处 `tr()` 调用(2 个 QFileDialog 标题 + 2 个 filter
串 + 1 条 status 文案)。它们**不能**直接搬进本文件 —— lupdate 静态扫描
按 **enclosing class name** 定 context,写在这里会变成 `ActionsMixin`,
下次跑 `lupdate -extensions py` 时 `.ts` 的 38 条 `MediaManagerWidget`
翻译会被标 `type="vanished"` → `lrelease` 产出空 `.qm` → i18n 全炸
(2026-09-07 已踩过,见 CHANGELOG「pyside6-lupdate 默认 extensions 不含
.py」事故;实证 `status_bar.py` 11 处双字面量 `QCoreApplication.translate`
从未被抽进 `.ts`)。

因此字面量留在 facade 的 `_tr_*()` accessor 里,mixin 侧只调 accessor
取**已翻译**字串 —— 遵循 `widgets/form_section.py:31-35` 的 house rule
「caller 提供已 tr() 过的字串,FormSection 不二次 tr」。详见
`media_manager_widget.py` 模块 docstring 的 i18n 硬约束章节。

MRO:`class MediaManagerWidget(FilterMixin, RenderMixin, ActionsMixin,
QWidget)` —— `QWidget` 在末尾。本 mixin 是 plain class,**不写
`__init__`**:实例字段全由 facade 单点赋值(沿用
`viewmodels/_base.py:94-103` 单点 init 惯例)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MessageDTO
from tgmonitor.ui.widgets._mm_base import _RowKey

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    _Base = QWidget
else:
    _Base = object


class ActionsMixin(_Base):
    """选中集 + 批量操作 + 导出 — 2026-10-20 plan-batch10-γ。

    公开 API 1:1 保留原 `MediaManagerWidget` 交互动作域:
    - 1 public method:`on_channel_cleared`
    - 10 private method:选中集 / 批量 / 导出 handler

    facade **不写同名 delegate** —— `tests/test_media_manager_widget.py`
    直接调 `widget._on_export_csv()`,MRO 直达本 mixin 方法,调用点 0 改动。
    """

    # ---- 内部:选中 / 批量 ----

    def _selected_keys(self) -> list[_RowKey]:
        """当前选中的所有 row → key list(按 list 内顺序)。"""
        keys: list[_RowKey] = []
        for it in self.list.selectedItems():
            row = self.list.row(it)
            if 0 <= row < len(self._last_keys):
                keys.append(self._last_keys[row])
        return keys

    def _on_selection_changed(self) -> None:
        """enable / disable 批量按钮(选 0 → 全灰;有 FAILED → retry 可点)。"""
        sel = self._selected_keys()
        has_sel = bool(sel)
        has_failed = False
        for k in sel:
            row = self._find_row_by_key(k)
            if row is None:
                continue
            if row[2].download_status == MediaDownloadStatus.FAILED:
                has_failed = True
                break
        self.btn_retry_sel.setEnabled(has_sel and has_failed)
        self.btn_delete_sel.setEnabled(has_sel)

    def _find_row_by_key(self, key: _RowKey) -> tuple[MessageDTO, int, MediaDTO] | None:
        for i, k in enumerate(self._last_keys):
            if k == key:
                return self._rows[i]
        return None

    def _on_select_all(self) -> None:
        self.list.selectAll()

    def _on_batch_retry(self) -> None:
        keys = [k for k in self._selected_keys() if self._row_is_failed(k)]
        if keys:
            self.batch_retry_requested.emit(keys)

    def _on_batch_delete(self) -> None:
        keys = self._selected_keys()
        if keys:
            self.batch_delete_requested.emit(keys)

    def _on_clear_channel(self) -> None:
        """2026-08-25 PR #4:取当前 filter 选中的 channel_id → emit 信号。

        MainWindow 接到信号做二次确认(QMessageBox.warning)再调
        vm.delete_by_channel。空 filter(All Channels)时直接 disable 按钮。
        """
        channel_id = self.cmb_channel.currentData()
        if channel_id is None:
            # `All Channels` / 未选 — 不允许整库清空
            return
        self.clear_channel_requested.emit(int(channel_id))

    def _on_export_csv(self) -> None:
        """2026-08-25 v1.3.0 PR #7:点 Export CSV → QFileDialog 选保存路径 →
        emit `export_csv_requested(str)`。
        """
        from datetime import datetime

        from PySide6.QtWidgets import QFileDialog

        default_name = f"media-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self,
            self._tr_export_csv_title(),
            default_name,
            self._tr_export_csv_filter(),
        )
        if not path:
            return
        self.export_csv_requested.emit(path)

    def _on_export_zip(self) -> None:
        """2026-09-01 v1.5.1 PR #B4:点 Export ZIP → QFileDialog 选 .zip 路径 →
        emit `export_zip_requested(path, include_thumbnails)`。

        `include_thumbnails` 来自同旁的 `chk_zip_thumbs` checkbox;默认
        不勾选(纯媒体包),勾上后打包 thumb_<arcname>。
        """
        from datetime import datetime

        from PySide6.QtWidgets import QFileDialog

        default_name = f"media-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        path, _ = QFileDialog.getSaveFileName(
            self,
            self._tr_export_zip_title(),
            default_name,
            self._tr_export_zip_filter(),
        )
        if not path:
            return
        self.export_zip_requested.emit(path, self.chk_zip_thumbs.isChecked())

    def on_channel_cleared(self, channel_id: int, deleted: int) -> None:
        """2026-08-25 PR #4:VM 反馈 → status bar + 自动 reload 当前 filter 列表。"""
        self.lbl_status.setText(
            self._tr_channel_cleared().format(cid=channel_id, n=deleted),
        )
        # 重 load 当前 filter(可能就是这个 channel)刷新 UI
        self.refresh_requested.emit()

    def _row_is_failed(self, key: _RowKey) -> bool:
        r = self._find_row_by_key(key)
        return r is not None and r[2].download_status == MediaDownloadStatus.FAILED