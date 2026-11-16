"""LIVE 多选浮层 — 选中态操作按钮。

2026-10-01 v1.12.x 从 `MainWindow` 抽出(原 `_SelectionToolbar`,`main_window.py:2119`):
默认 hidden,`selection_count_changed > 0` 时被 MainWindow show 出来。视觉与
QWidget 视觉更紧凑,与现有风格统一)。

按钮 9 颗:全选 / 反选 / 清除 / (分隔) / 标记已读 / 导出 / 删除 / 转发 / 钉选 / 表情回应。
计数 label 单独 objectName="selectionCountLabel",后续可加高亮样式。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget


class SelectionToolbar(QWidget):
    """LIVE 多选浮层。"""

    export_clicked = Signal()
    delete_clicked = Signal()
    mark_read_clicked = Signal()
    clear_clicked = Signal()
    select_all_clicked = Signal()
    invert_clicked = Signal()
    # 2026-09-09 v1.7.2:批量动作扩展 — 转发 / 钉选 / 表情回应。
    forward_clicked = Signal()
    pin_clicked = Signal()
    react_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("selectionToolbar")
        self.setFixedHeight(44)

        hbox = QHBoxLayout(self)
        hbox.setContentsMargins(16, 0, 16, 0)
        hbox.setSpacing(8)

        # 计数 label
        self._count_label = QLabel(self.tr("已选 0 条"))
        self._count_label.setObjectName("selectionCountLabel")
        hbox.addWidget(self._count_label)

        # 全选 / 反选 / 清除 — 操作类
        self._btn_select_all = QPushButton(self.tr("全选"))
        self._btn_select_all.setObjectName("selectionActionBtn")
        self._btn_select_all.clicked.connect(self.select_all_clicked.emit)
        hbox.addWidget(self._btn_select_all)

        self._btn_invert = QPushButton(self.tr("反选"))
        self._btn_invert.setObjectName("selectionActionBtn")
        self._btn_invert.clicked.connect(self.invert_clicked.emit)
        hbox.addWidget(self._btn_invert)

        self._btn_clear = QPushButton(self.tr("清除选择"))
        self._btn_clear.setObjectName("selectionActionBtn")
        self._btn_clear.clicked.connect(self.clear_clicked.emit)
        hbox.addWidget(self._btn_clear)

        hbox.addStretch(1)

        # 动作类 — 主按钮
        self._btn_mark_read = QPushButton(self.tr("✓ 标记已读"))
        self._btn_mark_read.setObjectName("selectionActionBtn")
        self._btn_mark_read.clicked.connect(self.mark_read_clicked.emit)
        hbox.addWidget(self._btn_mark_read)

        self._btn_export = QPushButton(self.tr("📤 导出选中"))
        self._btn_export.setObjectName("selectionActionBtn")
        self._btn_export.clicked.connect(self.export_clicked.emit)
        hbox.addWidget(self._btn_export)

        self._btn_delete = QPushButton(self.tr("🗑 删除选中"))
        self._btn_delete.setObjectName("selectionDeleteBtn")
        self._btn_delete.clicked.connect(self.delete_clicked.emit)
        hbox.addWidget(self._btn_delete)

        # 2026-09-09 v1.7.2:新增批量动作 — 转发 / 钉选 / 表情回应。
        self._btn_forward = QPushButton(self.tr("📤 转发到…"))
        self._btn_forward.setObjectName("selectionActionBtn")
        self._btn_forward.clicked.connect(self.forward_clicked.emit)
        hbox.addWidget(self._btn_forward)

        self._btn_pin = QPushButton(self.tr("📌 钉选"))
        self._btn_pin.setObjectName("selectionActionBtn")
        self._btn_pin.clicked.connect(self.pin_clicked.emit)
        hbox.addWidget(self._btn_pin)

        self._btn_react = QPushButton(self.tr("😀 表情回应…"))
        self._btn_react.setObjectName("selectionActionBtn")
        self._btn_react.clicked.connect(self.react_clicked.emit)
        hbox.addWidget(self._btn_react)

    def set_count(self, n: int) -> None:
        """更新计数 label — 由 `_on_live_selection_messages` 调。

        `tr("已选 {0} 条").format(n)` 不行(中英文数字混排),用 `%d` 兼容。
        """
        self._count_label.setText(self.tr("已选 %d 条") % n)
