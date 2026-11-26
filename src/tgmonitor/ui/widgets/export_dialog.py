# mypy: disable-error-code="attr-defined"
"""ExportDialog — 选择频道/时间/格式/输出路径,生成 ExportRequest。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.dto import ExportRequest
from tgmonitor.core.export import service as _export_service  # noqa: F401 — 触发 @exporter 注册
from tgmonitor.core.export.base import EXPORTERS
from tgmonitor.ui.widgets.form_row import combo_field, path_field

# 2026-11-25 P1-E:格式下拉 / 扩展名由 Exporter 注册表元数据驱动。
# 顶部 import service 确保内置 @exporter 装饰器执行完(注册 ready)。
# 2026-11-26:不再做模块级快照 —— 插件在 bootstrap 才激活,模块若先被
# import 就拿不到插件格式;改为每次构造 dialog 时从注册表实时派生。


class ExportDialog(QDialog):
    """导出对话框 — 选频道 / 时间 / 格式 / 输出路径,OK 时生成 ExportRequest。

    用户按 OK 后调 `request()` 取 ExportRequest(若校验失败用户从未按 OK
    则 assert 触发,正常 UI 路径下不会到)。

    2026-09-08 v1.7.0:扩展 `selected_messages` / `single_message_id` 入参 —
    LIVE 多选导出 / 单条导出场景下,频道已由 selection 决定,频道列表
    隐藏(只显示固定信息条「导出 N 条已选消息」)。`request()` 返回的
    ExportRequest 字段 `selected_messages` / `single_message_id` 同步设置。
    """

    def __init__(
        self,
        app,
        channel_ids: list[int],
        parent=None,
        *,
        selected_messages: list[tuple[int, int]] | None = None,
        single_message_id: int | None = None,
    ) -> None:
        """建 form + 默认文件名(`export-YYYYMMDD-HHMMSS.json`)。"""
        super().__init__(parent)
        self.app = app
        self._channel_ids = channel_ids
        self._selected_messages = selected_messages
        self._single_message_id = single_message_id
        self._req: ExportRequest | None = None
        # 2026-09-07 v1.6.8:所有用户可见字符串走 tr()。
        self.setWindowTitle(self.tr("导出"))
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
        # 2026-09-08 v1.7.0:多选导出 / 单条导出 — 频道已由 selection 决定,
        # 频道列表隐藏,显示固定信息条代替(不让用户改)。
        if self._selected_messages is not None:
            self.lst_channels.setVisible(False)
            # 替换 label — 频道 row 仍占位避免布局抖动,只在上面覆一行 hint
            hint = QLabel(
                self.tr("已选 %d 条消息(来自 %d 个频道)")
                % (
                    len(self._selected_messages),
                    len({cid for cid, _ in self._selected_messages}),
                )
            )
            hint.setProperty("role", "hint")
            form.insertRow(0, self.tr("导出范围:"), hint)
        elif self._single_message_id is not None:
            self.lst_channels.setVisible(False)
            hint = QLabel(self.tr("单条消息(#%d)") % (self._single_message_id,))
            hint.setProperty("role", "hint")
            form.insertRow(0, self.tr("导出范围:"), hint)
        else:
            form.addRow(self.tr("频道:"), self.lst_channels)

        # 时间范围(可选)
        self.in_from = QLineEdit()
        self.in_from.setPlaceholderText(self.tr("YYYY-MM-DD(可选)"))
        self.in_to = QLineEdit()
        self.in_to.setPlaceholderText(self.tr("YYYY-MM-DD(可选)"))
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.in_from)
        rl.addWidget(QLabel("~"))
        rl.addWidget(self.in_to)
        form.addRow(self.tr("时间范围:"), row)

        # 格式(combo_field 禁用滚轮切换 — 防滚动误改导出格式)
        # P1-E:从注册表取消息格式(排除 MEDIA_CSV,后者走不同 DTO)。
        # 2026-11-26:运行时派生,含插件注册的格式;combo_field 对非 tuple
        # 选项取 `.value`,纯 str 没有该属性,故显式传 (data, display) 对。
        self._formats = EXPORTERS.message_formats()
        self._format_ext = {fmt: EXPORTERS.extension(fmt) for fmt in self._formats}
        self.cmb_fmt = combo_field(form, self.tr("格式:"), [(fmt, fmt) for fmt in self._formats])

        # 选项 — HTML(内嵌缩略图)+ ZIP(打包缩略图)都用到;其它格式
        # 忽略该字段。2026-09-01 v1.5.1 PR #B4 加 ZIP 共享同一 checkbox。
        self.chk_thumbs = QCheckBox(self.tr("导出时包含缩略图(HTML / ZIP)"))
        self.chk_thumbs.setEnabled(False)  # 默认 JSON/CSV,选 HTML/ZIP 时启用
        form.addRow("", self.chk_thumbs)
        self.cmb_fmt.currentIndexChanged.connect(
            lambda i: self.chk_thumbs.setEnabled(
                EXPORTERS.supports_thumbnails(self.cmb_fmt.currentData())
            )
        )

        # 2026-09-10 v1.7.3:用户元数据开关 — 默认勾选(对收藏 / 标签 / 备注
        # 敏感的用户保留 ★ / 标签 / 备注);JSON / ZIP 不受影响(asdict 永远含)。
        # 只在 CSV / Markdown / HTML / MEDIA_CSV 时实际生效。
        self.chk_metadata = QCheckBox(self.tr("包含收藏 / 标签 / 备注"))
        self.chk_metadata.setChecked(True)
        form.addRow("", self.chk_metadata)

        # 输出路径(选文件而不是目录)
        self.in_path = path_field(
            form,
            self.tr("输出:"),
            "",
            file_mode=True,
            parent=self,
        )

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _set_default_filename(self) -> None:
        self.in_path.setText(
            f"./export-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            f"{self._format_ext.get('json', '.json')}"
        )

    def _on_ok(self) -> None:
        # 频道(多选导出 / 单条导出时 lst_channels 隐藏,直接走 self._*)
        ids: list[int] = []
        if self.lst_channels.isVisible():
            for i in range(self.lst_channels.count()):
                it = self.lst_channels.item(i)
                if it.checkState() == Qt.Checked:
                    ids.append(it.data(Qt.UserRole))
            if not ids:
                return
        else:
            ids = self._channel_ids
        # 格式
        fmt: str = self.cmb_fmt.currentData()
        out = self.in_path.text().strip()
        if not out:
            return
        # 自动补扩展名
        p = Path(out)
        if p.suffix == "":
            out = str(p.with_suffix(self._format_ext.get(fmt, "")))
        # 时间(多选 / 单条导出时,date 范围无意义 — 强制 None)
        if self._selected_messages is not None or self._single_message_id is not None:
            df: datetime | None = None
            dt: datetime | None = None
        else:
            df = self._parse_date(self.in_from.text().strip())
            dt = self._parse_date(self.in_to.text().strip())
        self._req = ExportRequest(
            channel_ids=ids,
            date_from=df,
            date_to=dt,
            format=fmt,
            out_path=out,
            # 2026-09-01 v1.5.1 PR #B4:ZIP 与 HTML 共用同一 checkbox,
            # 都把缩略图作为可选附件;其它格式该字段被 dispatcher 忽略。
            include_thumbnails=(EXPORTERS.supports_thumbnails(fmt) and self.chk_thumbs.isChecked()),
            # 2026-09-10 v1.7.3:用户元数据开关 — JSON / ZIP 永远含,
            # CSV / Markdown / HTML / MEDIA_CSV 据此加列 / 加 block。
            include_metadata=self.chk_metadata.isChecked(),
            # 2026-09-08 v1.7.0:多选 / 单条导出 — 透传字段给 ExportService。
            selected_messages=self._selected_messages,
            single_message_id=self._single_message_id,
        )
        self.accept()

    @staticmethod
    def _parse_date(s: str) -> datetime | None:
        """`YYYY-MM-DD` / `YYYY-MM-DDTHH:MM:SS` → aware datetime(本地时区);空 / 非法 → None。

        2.1.7 P1-6(同消息页日期预设):用户填的是本地时间,给 aware 值存储层
        才会正确转 UTC;此前 naive 值被当成 UTC 比较,东八区下导出范围
        整体偏移 8 小时。
        """
        if not s:
            return None
        for fmt_str in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s, fmt_str).astimezone()
            except ValueError:
                continue
        return None

    def request(self) -> ExportRequest:
        """取用户在 OK 时构造的 ExportRequest(只在 _on_ok 成功后才非空)。"""
        assert self._req is not None
        return self._req
