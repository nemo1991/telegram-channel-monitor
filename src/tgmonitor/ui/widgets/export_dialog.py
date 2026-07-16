"""ExportDialog — 选择频道/时间/格式/输出路径,生成 ExportRequest。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.dto import ExportFormat, ExportRequest

_FORMAT_EXT = {
    ExportFormat.JSON: ".json",
    ExportFormat.CSV: ".csv",
    ExportFormat.MARKDOWN: ".md",
    ExportFormat.HTML: ".html",
}


class ExportDialog(QDialog):
    def __init__(self, app, channel_ids: list[int], parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self._channel_ids = channel_ids
        self._req: ExportRequest | None = None
        self.setWindowTitle("导出")
        self._build()
        self._set_default_filename()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        form = QFormLayout()
        root.addLayout(form)

        # 频道(简化为只显示单选,生产可多选)
        self.lst_channels = QListWidget()
        for cid in self._channel_ids:
            it = QListWidgetItem(f"#{cid}")
            it.setData(Qt.UserRole, cid)
            it.setCheckState(Qt.Checked)
            self.lst_channels.addItem(it)
        form.addRow("频道:", self.lst_channels)

        # 时间范围(可选)
        self.in_from = QLineEdit()
        self.in_from.setPlaceholderText("YYYY-MM-DD(可选)")
        self.in_to = QLineEdit()
        self.in_to.setPlaceholderText("YYYY-MM-DD(可选)")
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.in_from)
        rl.addWidget(QLabel("~"))
        rl.addWidget(self.in_to)
        form.addRow("时间范围:", row)

        # 格式
        self.cmb_fmt = QComboBox()
        for f in ExportFormat:
            self.cmb_fmt.addItem(f.value, f)
        form.addRow("格式:", self.cmb_fmt)

        # 选项
        self.chk_thumbs = QCheckBox("HTML 导出时内嵌缩略图")
        self.chk_thumbs.setEnabled(False)  # 默认 JSON/CSV,选 HTML 时启用
        form.addRow("", self.chk_thumbs)
        self.cmb_fmt.currentIndexChanged.connect(
            lambda i: self.chk_thumbs.setEnabled(self.cmb_fmt.currentData() == ExportFormat.HTML)
        )

        # 输出路径
        path_row = QWidget()
        pl = QHBoxLayout(path_row)
        pl.setContentsMargins(0, 0, 0, 0)
        self.in_path = QLineEdit()
        self.btn_browse = QLabel("[浏览…]")
        self.btn_browse.setStyleSheet("color: #4a90e2; text-decoration: underline;")
        self.btn_browse.mousePressEvent = lambda _e: self._browse()  # type: ignore[assignment]
        pl.addWidget(self.in_path, 1)
        pl.addWidget(self.btn_browse)
        form.addRow("输出:", path_row)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _set_default_filename(self) -> None:
        self.in_path.setText(f"./export-{datetime.now().strftime('%Y%m%d-%H%M%S')}{_FORMAT_EXT[ExportFormat.JSON]}")

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "选择输出文件", self.in_path.text())
        if path:
            self.in_path.setText(path)

    def _on_ok(self) -> None:
        # 频道
        ids: list[int] = []
        for i in range(self.lst_channels.count()):
            it = self.lst_channels.item(i)
            if it.checkState() == Qt.Checked:
                ids.append(it.data(Qt.UserRole))
        if not ids:
            return
        # 格式
        fmt: ExportFormat = self.cmb_fmt.currentData()
        out = self.in_path.text().strip()
        if not out:
            return
        # 自动补扩展名
        p = Path(out)
        if p.suffix == "":
            out = str(p.with_suffix(_FORMAT_EXT[fmt]))
        # 时间
        df = self._parse_date(self.in_from.text().strip())
        dt = self._parse_date(self.in_to.text().strip())
        self._req = ExportRequest(
            channel_ids=ids,
            date_from=df,
            date_to=dt,
            format=fmt,
            out_path=out,
            include_thumbnails=(fmt == ExportFormat.HTML and self.chk_thumbs.isChecked()),
        )
        self.accept()

    @staticmethod
    def _parse_date(s: str) -> datetime | None:
        if not s:
            return None
        for fmt_str in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s, fmt_str)
            except ValueError:
                continue
        return None

    def request(self) -> ExportRequest:
        assert self._req is not None
        return self._req
