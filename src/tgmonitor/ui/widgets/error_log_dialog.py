"""错误日志 dialog — 状态栏铃铛入口。

2026-10-01 v1.12.x 从 `MainWindow` 抽出(原 `_ErrorLogDialog`,`main_window.py:2120`):
显示 `StatusBar._ErrorBellButton` 内部 ring buffer 内容(时间 / 来源 / 消息),
时间倒序,提供「清空日志」按钮。

依赖方向(couple inversion):原本 dialog 用 `hasattr(parent, '_clear_error_log')` 探针
回调 MainWindow;本次改为 callback 注入 — `__init__(entries, parent=None, *, on_clear)`
收可选 `on_clear: Callable[[], None]`,完全解耦 parent 类型(任何 QWidget 都行)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ErrorLogDialog(QDialog):
    """错误日志 dialog — 状态栏铃铛点击入口。"""

    def __init__(
        self,
        entries: list[tuple[datetime, str, str]],
        parent: QWidget | None = None,
        *,
        on_clear: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._entries = list(entries)
        self._on_clear_callback = on_clear
        self.setObjectName("errorLogDialog")
        self.setWindowTitle(self.tr("错误日志"))
        self.resize(640, 360)
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        header = QLabel(self.tr("最近 {n} 条错误(倒序):").format(n=len(self._entries)))
        header.setObjectName("errorLogHeader")
        root.addWidget(header)
        self.list = QListWidget()
        # 倒序:最新在最上面
        for when, source, msg in reversed(self._entries):
            text = f"{when.strftime('%H:%M:%S')}  [{source}]  {msg}"
            item = QListWidgetItem(text)
            self.list.addItem(item)
        if not self._entries:
            empty = QListWidgetItem(self.tr("(暂无错误)"))
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(empty)
        root.addWidget(self.list, 1)
        # 按钮行:清空 + 关闭
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._btn_clear = QPushButton(self.tr("清空日志"))
        self._btn_clear.setObjectName("errorLogClearBtn")
        self._btn_clear.clicked.connect(self._on_clear)
        btn_row.addWidget(self._btn_clear)
        btn_close = QPushButton(self.tr("关闭"))
        btn_close.setObjectName("errorLogCloseBtn")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

    def _on_clear(self) -> None:
        """清空日志 — 通过 callback 注入调外部(MainWindow)的清空入口。

        注入的 `on_clear` 通常指向 `MainWindow._clear_error_log`,负责:
        - 调 `status_bar.clear_error_log()` 清铃铛 ring buffer
        - 重设铃铛按钮 hidden

        本方法完成后,本地 `self._entries` 也清空(让 dialog 再次「清空」按钮
        触发时不报 stale)。
        """
        if self._on_clear_callback is not None:
            self._on_clear_callback()
        self._entries = []
        self.list.clear()
        empty = QListWidgetItem(self.tr("(暂无错误)"))
        empty.setFlags(Qt.ItemFlag.NoItemFlags)
        self.list.addItem(empty)
