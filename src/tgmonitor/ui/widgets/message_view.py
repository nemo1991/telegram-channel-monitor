"""MessageView — 实时消息流,带过滤 + 富格式。

存储:
  每条消息存为 QListWidgetItem,UserRole 存 msg_id,UserRole+1 存 MessageDTO。
  用 `hide()` / `show()` 控制可见性,实现过滤(避免重画已渲染的 row)。

格式(单行紧凑):
  ⏱ 14:23:10  [新闻]  👤 @author  #msg_id
    消息正文(可能多行)…
    📎 photo, document

空状态:首启 / 没订阅频道 / 还没消息到时居中显示「暂无消息」占位面板
(走 `form_row.empty_hint`),第一条数据到达自动隐藏。
"""
from __future__ import annotations

from datetime import UTC

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QListWidget, QListWidgetItem

from tgmonitor.core.dto import MessageDTO
from tgmonitor.ui.widgets.form_row import empty_hint


class MessageView(QListWidget):
    MAX_ITEMS = 1000
    _ROLE_MSG_ID = Qt.UserRole
    _ROLE_DTO = Qt.UserRole + 1

    # 用户点击一条消息 → emit MessageDTO 给详情面板
    message_selected = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setAlternatingRowColors(True)
        self.setUniformItemSizes(False)
        self.setWordWrap(True)
        # 去重表:key = (channel_id, telegram_msg_id) → list row index
        self._seen: dict[tuple[int, int], int] = {}
        # 频道 id → title;MainWindow 在 channels_changed 时调 set_channel_titles 同步
        self._channel_titles: dict[int, str] = {}
        # 过滤文本(空 = 不过滤)
        self._filter_text: str = ""

        self.itemClicked.connect(self._on_item_clicked)

        # 空状态占位(默认显示,首条消息到达自动隐藏)。
        # QListWidget 是 QAbstractScrollArea,接受 child widget 作为
        # overlay;setParent 后用 raise_() 把它顶到 viewport 上方。
        self._empty_overlay = empty_hint(
            icon="💬",
            title="暂无消息",
            hint="先去「频道」页双击订阅一个频道,\n"
                 "新消息会实时显示在这里。",
            parent=self,
        )
        self._empty_overlay.raise_()
        self._refresh_empty_state()

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt override
        super().resizeEvent(event)
        # 把 overlay 居中放在 list 上方 1/3 高度(视觉重心偏上,留底给 scrollbar)
        hint_size = self._empty_overlay.sizeHint()
        x = max(0, (self.width() - hint_size.width()) // 2)
        y = max(0, self.height() // 3 - hint_size.height() // 2)
        self._empty_overlay.setGeometry(x, y, hint_size.width(), hint_size.height())
        self._empty_overlay.raise_()

    def _refresh_empty_state(self) -> None:
        """count() == 0 → 显示 overlay,else 隐藏。"""
        self._empty_overlay.setVisible(self.count() == 0)
        self._empty_overlay.raise_()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        """点击一条消息 → 透传 MessageDTO。"""
        dto = item.data(self._ROLE_DTO)
        if isinstance(dto, MessageDTO):
            self.message_selected.emit(dto)

    def set_channel_titles(self, titles: dict[int, str]) -> None:
        self._channel_titles = dict(titles)

    def set_filter(self, text: str) -> None:
        """按文本过滤。空 = 显示全部。

        匹配规则:消息正文 OR 作者 OR 频道名 OR #msg_id(数字)。
        大小写不敏感。
        """
        text = text.strip().lower()
        self._filter_text = text
        for i in range(self.count()):
            item = self.item(i)
            if item is None:
                continue
            if not text:
                item.setHidden(False)
                continue
            dto = item.data(self._ROLE_DTO)
            if isinstance(dto, MessageDTO) and self._matches(dto, text):
                item.setHidden(False)
            else:
                item.setHidden(True)

    def _matches(self, m: MessageDTO, text: str) -> bool:
        if m.text and text in m.text.lower():
            return True
        if m.author and text in m.author.lower():
            return True
        title = self._channel_titles.get(m.channel_id, "")
        return bool(title and text in title.lower()) or (
            str(m.telegram_msg_id) == text or text.lstrip("#") == str(m.telegram_msg_id)
        )

    def append(self, m: MessageDTO) -> None:
        key = (m.channel_id, m.telegram_msg_id)
        if key in self._seen:
            # 已存在 — 文本可能更新(edit),替换那一行
            row = self._seen[key]
            item = self.item(row)
            if item is not None:
                item.setText(self._format(m))
                item.setData(self._ROLE_DTO, m)
                # 重检过滤
                if self._filter_text and not self._matches(m, self._filter_text):
                    item.setHidden(True)
            return
        text = self._format(m)
        item = QListWidgetItem(text)
        item.setData(self._ROLE_MSG_ID, m.telegram_msg_id)
        item.setData(self._ROLE_DTO, m)
        # 媒体行加底色
        if m.has_media:
            item.setBackground(QColor(232, 240, 248))
        self.insertItem(0, item)
        # 更新所有 row index(insertItem(0) 后 +1)
        for k in self._seen:
            self._seen[k] += 1
        self._seen[key] = 0
        # 应用过滤
        if self._filter_text and not self._matches(m, self._filter_text):
            item.setHidden(True)
        # 限制条数
        while self.count() > self.MAX_ITEMS:
            old_row = self.count() - 1
            old_item = self.takeItem(old_row)
            # 同步 _seen:任何指向 == old_row 的删除,> old_row 的 -= 1
            if old_item is not None:
                for k, v in list(self._seen.items()):
                    if v == old_row:
                        del self._seen[k]
                        break
            for k in self._seen:
                if self._seen[k] > old_row:
                    self._seen[k] -= 1
        self._refresh_empty_state()

    def clear_view(self) -> None:
        """外部调 — 清空列表 + 去重表(例如启动时)。"""
        self.clear()
        self._seen.clear()
        self._refresh_empty_state()

    def _format(self, m: MessageDTO) -> str:
        # 本地时区显示;m.date 是 **aware UTC**(来自 dto.py 默认工厂
        # `datetime.now(UTC)`,或 _map_message 的 `datetime.fromtimestamp(ts, UTC)`)。
        # 如果 m.date 没 tzinfo(从旧 JSONL 反序列化),fallback attach UTC tzinfo,
        # 然后 astimezone() 转本地;否则 astimezone() 把 naive 当成本地时间,
        # 不做时区转换。
        if m.date:
            dt_utc = m.date if m.date.tzinfo else m.date.replace(tzinfo=UTC)
            dt_local = dt_utc.astimezone()
            dt = dt_local.strftime("%H:%M:%S")
        else:
            dt = "[?]"
        # 频道名:有 title 用 title,没有用 #id 回退
        title = self._channel_titles.get(m.channel_id)
        ch_label = f"[{title}]" if title else f"[#{m.channel_id}]"
        # msg_id:在频道内的消息 id(Telegram 原始),不是 DB 自增
        msg_id = m.telegram_msg_id
        head = f"⏱ {dt}  {ch_label}  #{msg_id}"
        if m.author:
            head += f"  👤 {m.author}"
        body = m.text or ""
        if m.has_media:
            body += f"  📎 {','.join(med.type.value for med in m.media)}"
        return f"{head}\n  {body}"