"""MessageView — 实时消息流(简化为 QListWidget,按时间倒序追加)。

去重策略:`MessageReceived` 实时事件和 `load_recent_messages` 从 DB 重读
都通过 `append()` 进来 — 同一个 `(channel_id, telegram_msg_id)` 同一时刻
只保留一行。
"""
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
        # 去重表:key = (channel_id, telegram_msg_id) → list row index
        # 实时事件和历史重载都可能 emit 同一条;append() 时先查再决定 update / add。
        self._seen: dict[tuple[int, int], int] = {}

    def append(self, m: MessageDTO) -> None:
        key = (m.channel_id, m.telegram_msg_id)
        if key in self._seen:
            # 已存在 — 文本可能更新(edit),替换那一行
            row = self._seen[key]
            item = self.item(row)
            if item is not None:
                item.setText(self._format(m))
            return
        text = self._format(m)
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, m.telegram_msg_id)
        # 媒体行加底色
        if m.has_media:
            item.setBackground(QColor(40, 60, 80))
        self.insertItem(0, item)
        self._seen[key] = 0
        # 更新其它行的索引(insertItem(0) 后所有行 +1)
        for k in self._seen:
            if k != key:
                self._seen[k] += 1
        # 限制条数
        while self.count() > self.MAX_ITEMS:
            self.takeItem(self.count() - 1)

    def clear_view(self) -> None:
        """外部调 — 清空列表 + 去重表(例如启动时)。"""
        self.clear()
        self._seen.clear()

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
