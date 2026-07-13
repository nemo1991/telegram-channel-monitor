"""MessageView — 实时消息流(简化为 QListWidget,按时间倒序追加)。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QListWidget, QListWidgetItem

from tgmonitor.core.dto import MessageDTO


class MessageView(QListWidget):
    MAX_ITEMS = 1000

    def __init__(self) -> None:
        super().__init__()
        self.setAlternatingRowColors(True)
        self.setUniformItemSizes(False)

    def append(self, m: MessageDTO) -> None:
        text = self._format(m)
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, m.telegram_msg_id)
        # 媒体行加底色
        if m.has_media:
            item.setBackground(QColor(40, 60, 80))
        self.insertItem(0, item)
        # 限制条数
        while self.count() > self.MAX_ITEMS:
            self.takeItem(self.count() - 1)

    @staticmethod
    def _format(m: MessageDTO) -> str:
        dt = m.date.strftime("%Y-%m-%d %H:%M:%S") if m.date else "?"
        head = f"[{dt}] #{m.channel_id}"
        if m.author:
            head += f"  {m.author}"
        body = m.text or ""
        if m.has_media:
            body += f"  📎 {','.join(med.type.value for med in m.media)}"
        return f"{head}\n  {body}"
