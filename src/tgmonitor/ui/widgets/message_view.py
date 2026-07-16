"""MessageView — 实时消息流(简化为 QListWidget,按时间倒序追加)。

去重策略:`MessageReceived` 实时事件和 `load_recent_messages` 从 DB 重读
都通过 `append()` 进来 — 同一个 `(channel_id, telegram_msg_id)` 同一时刻
只保留一行。

渲染格式:
    [2026-07-15 21:50:10] [频道名] #msg_id  作者
      正文…

- 时间:**本地时区**(从 UTC 的 `m.date` 转换)
- 频道名:优先来自 `set_channel_titles()` 注册的 `id → title` 字典;
  没有则显示 `#-1001xxx`(保持回退,不要编)
- msg_id:用 `telegram_msg_id`(该频道内的消息 id),不是 DB 自增 `id`
"""
from __future__ import annotations

from datetime import UTC

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
        # 频道 id → title;MainWindow 在 channels_changed 时调 set_channel_titles 同步
        self._channel_titles: dict[int, str] = {}

    def set_channel_titles(self, titles: dict[int, str]) -> None:
        """外部(MainWindow)调 — 用 VM.known_channels 同步刷新标题表。

        设计:
        - 整张表替换(不增量),保证已退订频道的 title 也被清掉。
        - 已渲染的旧行不重画 — 标题变更只影响后续 append。
          (否则会触发全表 re-paint,千条历史时肉眼可见的卡顿)
        """
        self._channel_titles = dict(titles)

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

    def _format(self, m: MessageDTO) -> str:
        # 本地时区 — m.date 是 **naive UTC**(来自 _map_message 的 utcfromtimestamp
        # 或 dto.py 默认工厂 utcnow),必须先 attach UTC tzinfo 再 astimezone(),
        # 否则 astimezone() 会把 naive datetime 当成本地时间,不做时区转换。
        if m.date:
            dt_utc = m.date.replace(tzinfo=UTC)
            dt_local = dt_utc.astimezone()
            dt = dt_local.strftime("%Y-%m-%d %H:%M:%S")
        else:
            dt = "?"
        # 频道名:有 title 用 title,没有用 #id 回退
        title = self._channel_titles.get(m.channel_id)
        ch_label = f"[{title}]" if title else f"[#{m.channel_id}]"
        # msg_id:在频道内的消息 id(Telegram 原始),不是 DB 自增
        msg_id = m.telegram_msg_id
        head = f"[{dt}] {ch_label} #{msg_id}"
        if m.author:
            head += f"  {m.author}"
        body = m.text or ""
        if m.has_media:
            body += f"  📎 {','.join(med.type.value for med in m.media)}"
        return f"{head}\n  {body}"
