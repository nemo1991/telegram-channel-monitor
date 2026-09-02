# mypy: disable-error-code="attr-defined"
"""MessageView — 实时消息流,带过滤 + 富格式(QListView + delegate 重写)。

2026-09-02 v1.5.3 PR #D1:QListWidget → QListView + QAbstractListModel +
QStyledItemDelegate,真实 lazy render。**公开 API 全部保留**:`append` /
`set_messages` / `set_filter` / `set_channel_titles` / `remove_row` /
`clear_view` / `replace_message` / `update_media_status` / `message_selected`
signal / `_seen` / `count()` / `MAX_ITEMS`,`main_window._copy_current_message_text`
改用新 `current_message()` helper。

存储:
  每条消息存于 `MessageListModel._items: list[MessageDTO]`,`_index_of:
  dict[(channel_id, telegram_msg_id), row]` 提供 O(1) 去重 + 编辑/删除定位。
  `data(role)` 按需返 DTO / msg_id / hidden flag / formatted 富文本;
  delegate `paint()` 调 `index.data(FormattedRole)` 拿文本,QTextDocument
  渲染。hidden=True 的行 delegate 直接 return,不画。

格式(单行紧凑):
  ⏱ 14:23:10  [新闻]  👤 @author  #msg_id
    消息正文(可能多行)…
    📎 photo, document

空状态:首启 / 没订阅频道 / 还没消息到时居中显示「暂无消息」占位面板
(走 `form_row.empty_hint`),第一条数据到达自动隐藏。
"""

from __future__ import annotations

from datetime import UTC

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QTextDocument
from PySide6.QtWidgets import QListView, QStyledItemDelegate, QStyleOptionViewItem

from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MessageDTO
from tgmonitor.ui.widgets.form_row import empty_hint

# ============================================================
# MessageListModel — QAbstractListModel 子类 + 业务方法
# ============================================================


class MessageListModel(QAbstractListModel):
    """消息列表 model — DTO list + _seen dict + filter state + channel_titles。

    2026-09-02 v1.5.3 PR #D1:`QListWidgetItem` 内部存储 → 真实 Qt model,
    delegate 按需 paint。**对外通过 role 协议暴露数据**:`data(idx, role)`
    按 role 返 DTO / msg_id / hidden flag / formatted 富文本 / has_media。
    """

    DtoRole = Qt.UserRole + 1
    MsgIdRole = Qt.UserRole + 2
    HiddenRole = Qt.UserRole + 3
    FormattedRole = Qt.UserRole + 4
    HasMediaRole = Qt.UserRole + 5

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[MessageDTO] = []
        self._index_of: dict[tuple[int, int], int] = {}
        self._channel_titles: dict[int, str] = {}
        self._filter_text: str = ""

    # ---- Qt model 接口 ----

    def rowCount(  # noqa: N802 — Qt override
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()
    ) -> int:
        if parent.isValid():
            return 0
        return len(self._items)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._items):
            return None
        m = self._items[row]
        if role == self.DtoRole:
            return m
        if role == self.MsgIdRole:
            return m.telegram_msg_id
        if role == self.HasMediaRole:
            return bool(m.has_media)
        if role == self.HiddenRole:
            if not self._filter_text:
                return False
            return not self._matches(m, self._filter_text)
        if role == self.FormattedRole or role == Qt.DisplayRole:
            return self._format(m)
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    # ---- 业务接口(给 MessageView 调) ----

    def append(self, m: MessageDTO) -> None:
        """实时追加一条 — 已存在替换,否则插入头部(newest-first)。"""
        key = (m.channel_id, m.telegram_msg_id)
        if key in self._index_of:
            # 已存在 — 文本可能更新(edit),替换那一行
            row = self._index_of[key]
            self._items[row] = m
            idx = self.index(row, 0)
            self.dataChanged.emit(
                idx,
                idx,
                [self.DtoRole, self.FormattedRole, self.HiddenRole, self.HasMediaRole],
            )
            return
        # 插入头部
        self.beginInsertRows(QModelIndex(), 0, 0)
        self._items.insert(0, m)
        # _index_of 全部 +1
        for k in self._index_of:
            self._index_of[k] += 1
        self._index_of[key] = 0
        self.endInsertRows()
        # MAX_ITEMS 截断(尾部删)
        while len(self._items) > MessageView.MAX_ITEMS:
            self._truncate_tail()

    def _truncate_tail(self) -> None:
        """删尾部一行 — 同步 _index_of 偏移。"""
        last = len(self._items) - 1
        if last < 0:
            return
        self.beginRemoveRows(QModelIndex(), last, last)
        # 找 key → row == last
        removed_key = next((k for k, v in self._index_of.items() if v == last), None)
        if removed_key is not None:
            del self._index_of[removed_key]
        del self._items[last]
        self.endRemoveRows()

    def remove_by_key(self, channel_id: int, telegram_msg_id: int) -> None:
        """按 (channel_id, telegram_msg_id) 删一行 — 找不到 idempotent。"""
        key = (channel_id, telegram_msg_id)
        row = self._index_of.pop(key, None)
        if row is None:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._items[row]
        # 后面 row index -1
        for k in self._index_of:
            if self._index_of[k] > row:
                self._index_of[k] -= 1
        self.endRemoveRows()

    def reset(self, messages: list[MessageDTO]) -> None:
        """整批替换(给 set_messages / clear_view 用)— atomic reset。

        `messages` 按 date ASC 传入(latest 在末尾)— model 保持传入顺序,
        caller 负责保证 newest-last。**不要 reversed** —— 反向迭代会让
        最旧消息顶到 row 0,顺序颠倒(同 v1.5.2 PR #B5 set_messages 语义)。
        """
        self.beginResetModel()
        self._items = list(messages)
        self._index_of = {(m.channel_id, m.telegram_msg_id): i for i, m in enumerate(self._items)}
        # MAX_ITEMS 截断(尾部删,不走 beginRemoveRows 因为已在 resetModel 中)
        while len(self._items) > MessageView.MAX_ITEMS:
            self._truncate_tail_inplace()
        self.endResetModel()

    def _truncate_tail_inplace(self) -> None:
        """reset 中用 — 不走 beginRemoveRows/endRemoveRows(已在 resetModel 中)。"""
        if not self._items:
            return
        last = len(self._items) - 1
        # 找 key → row == last
        removed_key = next((k for k, v in self._index_of.items() if v == last), None)
        if removed_key is not None:
            del self._index_of[removed_key]
        del self._items[last]

    def set_filter(self, text: str) -> None:
        """设过滤文本 — 所有 row 的 HiddenRole 变化 → emit dataChanged。"""
        self._filter_text = text.strip().lower()
        if self.rowCount() == 0:
            return
        top = self.index(0, 0)
        bottom = self.index(self.rowCount() - 1, 0)
        self.dataChanged.emit(top, bottom, [self.HiddenRole])

    def set_channel_titles(self, titles: dict[int, str]) -> None:
        """设 channel_titles + FormattedRole 全部失效(影响 head 频道名)。"""
        self._channel_titles = dict(titles)
        if self.rowCount() == 0:
            return
        top = self.index(0, 0)
        bottom = self.index(self.rowCount() - 1, 0)
        self.dataChanged.emit(top, bottom, [self.FormattedRole])

    def replace_message(self, msg: MessageDTO) -> None:
        """编辑事件:按 key 找 row,重 format + 重检 filter。"""
        key = (msg.channel_id, msg.telegram_msg_id)
        row = self._index_of.get(key)
        if row is None:
            # 罕见:编辑事件先于 new message 到达 — 当新增处理
            self.append(msg)
            return
        self._items[row] = msg
        idx = self.index(row, 0)
        self.dataChanged.emit(
            idx,
            idx,
            [self.DtoRole, self.FormattedRole, self.HiddenRole, self.HasMediaRole],
        )

    def update_media_status(self, channel_id: int, telegram_msg_id: int, media: MediaDTO) -> None:
        """异步下载结束回调:更新 DTO.media + 重 format。"""
        row = self._index_of.get((channel_id, telegram_msg_id))
        if row is None:
            return
        dto = self._items[row]
        for i, med in enumerate(dto.media):
            if med is media or (
                media.telegram_file_id and med.telegram_file_id == media.telegram_file_id
            ):
                dto.media[i] = media
                break
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [self.DtoRole, self.FormattedRole])

    # ---- 过滤 / 格式化工具 ----

    def _matches(self, m: MessageDTO, text: str) -> bool:
        """匹配规则:正文 / 作者 / 频道名 / #msg_id(大小写不敏感)。"""
        if m.text and text in m.text.lower():
            return True
        if m.author and text in m.author.lower():
            return True
        title = self._channel_titles.get(m.channel_id, "")
        if title and text in title.lower():
            return True
        return str(m.telegram_msg_id) == text or text.lstrip("#") == str(m.telegram_msg_id)

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
            parts = []
            for med in m.media:
                label = med.type.value
                # 下载状态标记:⏳ 下载中 / ❌ 失败 / ✓ 已下载(PENDING 不加后缀)
                if med.download_status == MediaDownloadStatus.DOWNLOADING:
                    label += "⏳"
                elif med.download_status == MediaDownloadStatus.FAILED:
                    label += "❌"
                elif med.download_status == MediaDownloadStatus.DONE:
                    label += "✓"
                parts.append(label)
            body += f"  📎 {','.join(parts)}"
        return f"{head}\n  {body}"


# ============================================================
# MessageItemDelegate — paint + sizeHint(QTextDocument)
# ============================================================


class MessageItemDelegate(QStyledItemDelegate):
    """2026-09-02 v1.5.3 PR #D1:lazy paint delegate。

    - hidden=True → 不画(节省 paint 开销)
    - media 行 → fillRect 底色(232,240,248)
    - 普通行 → QTextDocument 渲 rich text(支持 word wrap)
    """

    MEDIA_BG = QColor(232, 240, 248)

    def paint(
        self,
        painter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        if index.data(MessageListModel.HiddenRole):
            return
        has_media = bool(index.data(MessageListModel.HasMediaRole))
        if has_media:
            painter.fillRect(option.rect, self.MEDIA_BG)
        text = index.data(MessageListModel.FormattedRole) or ""
        if not text:
            return
        doc = QTextDocument()
        doc.setDefaultFont(option.font)
        doc.setHtml(self._plain_to_html(text))
        painter.save()
        painter.translate(option.rect.topLeft())
        doc.setTextWidth(option.rect.width())
        doc.drawContents(painter)
        painter.restore()

    def sizeHint(  # noqa: N802 — Qt override
        self,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ):
        text = index.data(MessageListModel.FormattedRole) or ""
        if not text:
            return super().sizeHint(option, index)
        doc = QTextDocument()
        doc.setDefaultFont(option.font)
        doc.setHtml(self._plain_to_html(text))
        doc.setTextWidth(option.rect.width() if option.rect.width() > 0 else 280)
        # 高度 = 内容 + 上下各 4px padding
        from PySide6.QtCore import QSize

        return QSize(int(doc.idealWidth()), int(doc.size().height()) + 8)

    @staticmethod
    def _plain_to_html(text: str) -> str:
        """QListWidget.setText 走 plain text;delegate 走 QTextDocument
        需 HTML。换行符 `\n` → `<br>`,`&` `<` `>` 转义避免被当 HTML 解析。

        旧实现 `QListWidgetItem.text` 自动处理换行 + escape;这里需要手动。
        """
        esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<pre style='margin:0; padding:0;'>{esc.replace(chr(10), '<br>')}</pre>"


# ============================================================
# MessageView — QListView 子类
# ============================================================


class MessageView(QListView):
    """实时消息流 — QListView + MessageListModel + MessageItemDelegate。

    2026-09-02 v1.5.3 PR #D1:从 QListWidget 子类改为 QListView + 自定义
    model + delegate。公开 API 全部保留(append / set_messages /
    set_filter / set_channel_titles / remove_row / clear_view /
    replace_message / update_media_status / message_selected signal /
    count / MAX_ITEMS / _format),内部 model + delegate 换皮。
    """

    MAX_ITEMS = 1000

    # 用户点击一条消息 → emit MessageDTO 给详情面板
    message_selected = Signal(object)

    def __init__(self) -> None:
        """初始化 model + delegate + channel_titles + filter + empty overlay。"""
        super().__init__()
        self.setAlternatingRowColors(True)
        self.setUniformItemSizes(False)  # delegate 动态 size
        self.setWordWrap(True)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)

        self._model = MessageListModel(self)
        self.setModel(self._model)

        self._delegate = MessageItemDelegate(self)
        self.setItemDelegate(self._delegate)

        # 点击 → 取 DTO → emit
        self.clicked.connect(self._on_clicked)

        # 空状态占位(默认显示,首条消息到达自动隐藏)
        self._empty_overlay = empty_hint(
            icon="💬",
            title="暂无消息",
            hint="先去「频道」页双击订阅一个频道,\n新消息会实时显示在这里。",
            parent=self,
        )
        self._empty_overlay.raise_()
        self._refresh_empty_state()
        # model 行数变化时刷新 overlay
        self._model.rowsInserted.connect(self._refresh_empty_state)
        self._model.rowsRemoved.connect(self._refresh_empty_state)
        self._model.modelReset.connect(self._refresh_empty_state)

    # ---- 公开 API(全部保留) ----

    def append(self, m: MessageDTO) -> None:
        """实时追加一条 — 委托 model。"""
        self._model.append(m)

    def set_messages(self, messages: list[MessageDTO]) -> None:
        """VM 搜索结果批量替换 — 与 `clear_view` + 多次 `append` 等价。

        `messages` 通常按 date ASC 从 storage 拉回 — 正常顺序逐条 `append()`,
        最新一条最后 append → `append` 走 `model.append` 的 `beginInsertRows(0,0)`
        自然落到 row 0(newest-first),与 LIVE 流约定一致。**不要 reversed** ——
        反向迭代会让最旧消息最后 append → 顶到 row 0,顺序颠倒。

        race 处理:live `MessageReceived` 在 `set_messages` 期间到达 → 落到
        `_index_of` 表已存在的 key 上,`append()` 的「已存在替换」分支
        正确处理(替换 text 不增 row)。这是预期行为。

        空列表 = 清空视图 + `_index_of` 表(同 `clear_view()`)。
        """
        self.clear_view()
        # 保持 newest-first:`messages` 按 date ASC 拉回 — 正常顺序逐条
        # `append()`,最新一条最后 append → 走 model.append 的 beginInsertRows(0,0) →
        # 自然落到 row 0(`_index_of[key] = 0`)。`reversed` 会反过来,旧消息
        # 反而顶到 row 0 — 错。
        for m in messages:
            self.append(m)
        # 截断后 apply 现有 filter(set_filter 在每条 append 时已逐条应用,
        # 但 set_messages 整体替换完后再保险跑一次 — 处理 filter 在中途变化的情况)
        if self._model._filter_text:
            self._model.set_filter(self._model._filter_text)

    def set_channel_titles(self, titles: dict[int, str]) -> None:
        """外部注入频道 id → title 映射 — 委托 model。"""
        self._model.set_channel_titles(titles)

    def set_filter(self, text: str) -> None:
        """按文本过滤。空 = 显示全部。"""
        self._model.set_filter(text)

    def remove_row(self, channel_id: int, telegram_msg_id: int) -> None:
        """删一行 — 委托 model。"""
        self._model.remove_by_key(channel_id, telegram_msg_id)

    def clear_view(self) -> None:
        """清空列表 — model reset + empty overlay 显示。"""
        self._model.reset([])

    def replace_message(self, msg: MessageDTO) -> None:
        """编辑事件触发 — 委托 model。"""
        self._model.replace_message(msg)

    def update_media_status(self, channel_id: int, telegram_msg_id: int, media: MediaDTO) -> None:
        """异步下载结束回调 — 委托 model。"""
        self._model.update_media_status(channel_id, telegram_msg_id, media)

    def count(self) -> int:
        """行数 — 兼容 QListWidget.count()。"""
        return self._model.rowCount()

    def current_message(self) -> MessageDTO | None:
        """2026-09-02 v1.5.3 PR #D1:`main_window._copy_current_message_text` 用。

        替代旧 `currentItem().data(Qt.UserRole)`。
        """
        idx = self.currentIndex()
        if not idx.isValid():
            return None
        return self._model.data(idx, MessageListModel.DtoRole)

    def _format(self, m: MessageDTO) -> str:
        """shim — 既有测试 `view._format(m)` 走这里(内部调 model._format)。"""
        return self._model._format(m)

    # ---- 内部 ----

    def _on_clicked(self, index: QModelIndex) -> None:
        dto = self._model.data(index, MessageListModel.DtoRole)
        if isinstance(dto, MessageDTO):
            self.message_selected.emit(dto)

    def _refresh_empty_state(self, *_args) -> None:
        """count() == 0 → 显示 overlay,else 隐藏。

        signal 回调签名兼容 `rowsInserted(parent, first, last)` /
        `rowsRemoved(parent, first, last)` / `modelReset()`,所以接 *args。
        """
        self._empty_overlay.setVisible(self.count() == 0)
        self._empty_overlay.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt override
        """窗口尺寸变 → overlay 重新居中(视觉重心偏上 1/3 高度)。"""
        super().resizeEvent(event)
        hint_size = self._empty_overlay.sizeHint()
        x = max(0, (self.width() - hint_size.width()) // 2)
        y = max(0, self.height() // 3 - hint_size.height() // 2)
        self._empty_overlay.setGeometry(x, y, hint_size.width(), hint_size.height())
        self._empty_overlay.raise_()
