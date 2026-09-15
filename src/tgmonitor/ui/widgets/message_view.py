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

from collections import deque
from datetime import UTC

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QPalette, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListView,
    QMenu,
    QPushButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MessageDTO, ReactionDTO
from tgmonitor.ui.widgets._reaction_format import format_reactions_short
from tgmonitor.ui.widgets.form_row import empty_hint

# ============================================================
# MessageListModel — QAbstractListModel 子类 + 业务方法
# ============================================================


def _format_meta_icons(m: MessageDTO) -> str:
    """2026-09-10 v1.7.3:把 ★ / 🏷 / 📝 / 📌 拼成一行字符串;无 metadata 返空串。

    顺序固定:★ → 🏷 → 📝 → 📌(★ 最重要,放最前;📌 server-side state 放最后)。
    tags 用 `,` 分隔(与 LIVE 行 hover / MessageDetail 一致);notes 超过
    30 字符截断加 `…`(行高可控,避免 100 字备注撑爆单行)。

    2026-09-11 v1.7.4:reactions 拼到末尾(`🔥 5  👍 3`)— 由
    MessageInteractionsChanged event 触发本地刷新,实时性受限于 TDLib
    `updateMessageInteractionInfo` 推送(bots-only `updateMessageReactions`
    user client 不收)。
    """
    parts: list[str] = []
    if m.is_favorite:
        parts.append("★")
    if m.tags:
        parts.append(f"🏷{','.join(m.tags)}")
    if m.notes:
        snippet = m.notes if len(m.notes) <= 30 else m.notes[:30] + "…"
        parts.append(f"📝{snippet}")
    if m.is_pinned:
        parts.append("📌")
    if m.reactions:
        rx = format_reactions_short(m.reactions, max_show=3, mark_chosen=False)
        if rx:
            parts.append(rx)
    return " ".join(parts)


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
        # 2026-09-15 v1.7.5 PR #9 perf:deque 替 list,O(1) head-insert + O(1) tail pop。
        # `deque[i]` 索引是 O(1) 均摊,可直接当 list 用。appendleft 给最新消息落 row 0
        # (newest-first 语义),`deque.pop()`(tail side)给 `_truncate_tail` 删最旧。
        self._items: deque[MessageDTO] = deque()
        # key → row(`_items` 物理位置 = 逻辑 row,newest 在 0)
        self._index_of: dict[tuple[int, int], int] = {}
        # 2026-09-15 v1.7.5 PR #9 perf:FormattedRole 缓存。`_format()` 涉及
        # datetime.astimezone + strftime + 多 f-string,每个 paint 触发 60+ 次。
        # 缓存 keyed on (cid, mid),数据变更时由 model 显式 pop(详见各
        # mutation 方法的 `_format_cache.pop(...)`)。
        self._format_cache: dict[tuple[int, int], str] = {}
        # 2026-09-03 v1.5.4 PR #P1 已删:`_row_to_key` 反向索引。2026-09-15
        # PR #9 改成 deque 后,`_items[last]` 拿 key 已是 O(1),无需 mirror 索引 —
        # 维护成本降低,append 路径去掉 O(N log N) sort + O(N) shift,降为
        # 单纯 O(N) `_index_of` bump(bump 仍是 O(N),因为所有现有 row index +1)
        # + O(1) deque.appendleft / O(1) deque.pop(头/尾插入删除都是常数时间)。
        self._channel_titles: dict[int, str] = {}
        self._filter_text: str = ""
        # 2026-09-14 v1.7.5 PR #8:3 个用户元数据过滤维度(AND 语义)。
        self._favorite_only: bool = False
        self._tag_only: bool = False
        self._pinned_only: bool = False
        # 2026-09-15 v1.7.5 PR #9 perf:`set_messages` / `clear_view` 走
        # `reset()` 时整批消息换新 → delegate 的 `_doc_cache` / `_size_hint_cache`
        # 失效。否则 paint 命中过期的 (cid, mid) → 显示老消息文本。
        # 调用方(MessageView.__init__)注入;默认 None = 单测无 delegate。
        self._delegate: MessageItemDelegate | None = None

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
            return not self._matches(m)
        if role == self.FormattedRole or role == Qt.DisplayRole:
            # 2026-09-15 v1.7.5 PR #9 perf:FormattedRole 缓存 — paint 热路径
            # 避免每帧重 _format(datetime.astimezone + 8 个 f-string)。
            # Cache 由 mutation 路径(replace / update_media_status /
            # refresh_reactions / append-dedup / remove / reset)显式 invalidate。
            key = (m.channel_id, m.telegram_msg_id)
            cached = self._format_cache.get(key)
            if cached is not None:
                return cached
            result = self._format(m)
            self._format_cache[key] = result
            return result
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    # ---- 业务接口(给 MessageView 调) ----

    def append(self, m: MessageDTO) -> None:
        """实时追加一条 — 已存在替换,否则插入头部(newest-first)。

        2026-09-15 v1.7.5 PR #9 perf:`deque.appendleft` 替 `list.insert(0)` 拿到
        O(1) head-insert(原 list.insert(0) 是 O(N) Python element shift)。
        `_index_of` bump 仍是 O(N)(所有现有 row 索引 +1),但去掉了 `_row_to_key`
        O(N log N) sort + O(N) shift — 单条 append 总开销从 O(N log N) 降到 O(N)。
        """
        key = (m.channel_id, m.telegram_msg_id)
        if key in self._index_of:
            # 已存在 — 文本可能更新(edit),替换那一行
            row = self._index_of[key]
            self._items[row] = m  # deque 支持 __setitem__
            self._format_cache.pop(key, None)  # PR #9:失效缓存
            idx = self.index(row, 0)
            self.dataChanged.emit(
                idx,
                idx,
                [self.DtoRole, self.FormattedRole, self.HiddenRole, self.HasMediaRole],
            )
            return
        # 插入头部 — O(1) via deque.appendleft
        self.beginInsertRows(QModelIndex(), 0, 0)
        self._items.appendleft(m)
        # _index_of 全部 +1(现有 row 都向后挪 1)— O(N) bump
        for k in self._index_of:
            self._index_of[k] += 1
        self._index_of[key] = 0
        self.endInsertRows()
        # MAX_ITEMS 截断(尾部删,走 O(1) deque.pop())
        while len(self._items) > MessageView.MAX_ITEMS:
            self._truncate_tail()

    def _truncate_tail(self) -> None:
        """删尾部一行(物理位置 len-1 = 最旧)— 不需要 _index_of shift。

        newest-first 布局:`_items[0]` = newest(刚 appendleft 进来的),
        `_items[len-1]` = oldest。MAX_ITEMS 超限要砍最旧 = 删 row len-1。
        删最末 row 不影响其他 row 的 index(`_index_of` 存的值不变)。
        """
        if not self._items:
            return
        last = len(self._items) - 1
        self.beginRemoveRows(QModelIndex(), last, last)
        # O(1) 拿 key:最旧的在 `_items[last]`(deque[-1] 也是 O(1))。
        oldest = self._items[last]
        removed_key = (oldest.channel_id, oldest.telegram_msg_id)
        del self._index_of[removed_key]
        self._format_cache.pop(removed_key, None)  # PR #9:失效缓存
        self._items.pop()  # O(1) deque tail pop — 删最末 row
        self.endRemoveRows()

    def remove_by_key(self, channel_id: int, telegram_msg_id: int) -> None:
        """按 (channel_id, telegram_msg_id) 删一行 — 找不到 idempotent。

        2026-09-15 PR #9:deque 不支持 `del d[i]`,改用 slice rebuild。仍是 O(N),
        但 remove_by_key 不是热路径(MessageDeleted 频率低)。
        """
        key = (channel_id, telegram_msg_id)
        row = self._index_of.pop(key, None)
        if row is None:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        # 重建 deque 跳过 `row` 位 — O(N)
        self._items = deque(m for i, m in enumerate(self._items) if i != row)
        self._format_cache.pop(key, None)  # PR #9:失效缓存
        # row > row 的 entry -1
        for k in self._index_of:
            if self._index_of[k] > row:
                self._index_of[k] -= 1
        self.endRemoveRows()

    def reset(self, messages: list[MessageDTO]) -> None:
        """整批替换(给 set_messages / clear_view 用)— atomic reset。

        `messages` 按 date ASC 传入(latest 在末尾)— model 保持传入顺序,
        caller 负责保证 newest-last。**不要 reversed** —— 反向迭代会让
        最旧消息顶到 row 0,顺序颠倒(同 v1.5.2 PR #B5 set_messages 语义)。

        2026-09-15 v1.7.5 PR #9:`_items` 改 deque,`_row_to_key` 删,
        `_format_cache` 全清(`messages` 列表里的 key 集合变了),
        并通知 delegate 清 `_doc_cache` / `_size_hint_cache`。
        """
        self.beginResetModel()
        self._items = deque(messages)
        self._index_of = {(m.channel_id, m.telegram_msg_id): i for i, m in enumerate(self._items)}
        self._format_cache.clear()  # PR #9:cache 全清(消息集合可能全换)
        # PR #9:delegate cache 全清 — 旧 (cid, mid) 文档 + sizeHint 都失效。
        if self._delegate is not None:
            self._delegate.clear_caches()
        # MAX_ITEMS 截断(尾部删,不走 beginRemoveRows 因为已在 resetModel 中)
        while len(self._items) > MessageView.MAX_ITEMS:
            self._truncate_tail_inplace()
        self.endResetModel()

    def set_delegate(self, delegate: MessageItemDelegate | None) -> None:
        """PR #9:注入 delegate 引用 — `reset()` 时通知其清 cache。

        `MessageView.__init__` 里 model 与 delegate 都建好后调用一次。
        单测里没 delegate 时可保持 None,不影响行为。
        """
        self._delegate = delegate

    def _truncate_tail_inplace(self) -> None:
        """reset 中用 — 不走 beginRemoveRows/endRemoveRows(已在 resetModel 中)。

        PR #9:对应 `_truncate_tail`,但省略 Qt signal。`_items.pop()` 删最旧。
        """
        if not self._items:
            return
        oldest = self._items[len(self._items) - 1]
        removed_key = (oldest.channel_id, oldest.telegram_msg_id)
        del self._index_of[removed_key]
        self._format_cache.pop(removed_key, None)
        self._items.pop()  # O(1) deque tail pop — 删最旧

    def set_filter(
        self,
        text: str = "",
        *,
        favorite_only: bool = False,
        tag_only: bool = False,
        pinned_only: bool = False,
    ) -> None:
        """设过滤条件 — 所有 row 的 HiddenRole 变化 → emit dataChanged。

        向后兼容:旧调用 `set_filter(text)` 仍能用(默认 favorite/tag/pinned=False)。

        2026-09-14 v1.7.5 PR #8:加 favorite/tag/pinned 3 个 bool kwarg(AND
        语义)。SearchBar ★/🏷/📌 toggle 状态走这条路径 — 即时 narrow LIVE
        流已加载消息,无 IO。`search_messages` 异步路径另走 storage 服务端过滤。
        """
        self._filter_text = text.strip().lower()
        self._favorite_only = favorite_only
        self._tag_only = tag_only
        self._pinned_only = pinned_only
        if self.rowCount() == 0:
            return
        top = self.index(0, 0)
        bottom = self.index(self.rowCount() - 1, 0)
        self.dataChanged.emit(top, bottom, [self.HiddenRole])

    def set_channel_titles(self, titles: dict[int, str]) -> None:
        """设 channel_titles + FormattedRole 全部失效(影响 head 频道名)。

        PR #9:channel titles 影响每行的 head 频道名 → 全表 FormattedRole 都失效,
        缓存全清。
        """
        self._channel_titles = dict(titles)
        self._format_cache.clear()  # PR #9:全表失效
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
        self._format_cache.pop(key, None)  # PR #9:失效缓存
        idx = self.index(row, 0)
        self.dataChanged.emit(
            idx,
            idx,
            [self.DtoRole, self.FormattedRole, self.HiddenRole, self.HasMediaRole],
        )

    def update_media_status(self, channel_id: int, telegram_msg_id: int, media: MediaDTO) -> None:
        """异步下载结束回调:更新 DTO.media + 重 format。"""
        key = (channel_id, telegram_msg_id)
        row = self._index_of.get(key)
        if row is None:
            return
        dto = self._items[row]
        for i, med in enumerate(dto.media):
            if med is media or (
                media.telegram_file_id and med.telegram_file_id == media.telegram_file_id
            ):
                dto.media[i] = media
                break
        self._format_cache.pop(key, None)  # PR #9:失效缓存
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [self.DtoRole, self.FormattedRole])

    def refresh_reactions(
        self,
        channel_id: int,
        telegram_msg_id: int,
        reactions: list[ReactionDTO] | None,
    ) -> None:
        """2026-09-11 v1.7.4:TDLib `MessageInteractionsChanged` 触发 — 局部刷新一行 reactions。

        TDLib `updateMessageReactions` 是 bots-only,user client 收不到 —
        本方法依赖 `updateMessageInteractionInfo`(`MessageInteractionsChanged.reactions`),
        服务端推送策略决定实时性。

        Args:
            channel_id:目标消息频道。
            telegram_msg_id:目标消息 id。
            reactions:新 reactions 列表(`None` 表示服务端「无变化」,
                直接忽略,避免无谓 refresh)。
        """
        if reactions is None:
            return
        row = self._index_of.get((channel_id, telegram_msg_id))
        if row is None:
            return
        # MessageDTO 是 mutable dataclass,直接赋值即可。
        self._items[row].reactions = list(reactions) if reactions else None
        self._format_cache.pop((channel_id, telegram_msg_id), None)  # PR #9:失效缓存
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [self.DtoRole, self.FormattedRole])

    def dto_by_key(self, channel_id: int, telegram_msg_id: int) -> MessageDTO | None:
        """2026-09-11 v1.7.4:按 (cid, mid) 拿当前 DTO 引用 — 不存在返 None。"""
        row = self._index_of.get((channel_id, telegram_msg_id))
        if row is None:
            return None
        return self._items[row]

    # ---- 过滤 / 格式化工具 ----

    def _matches(self, m: MessageDTO) -> bool:
        """匹配规则:text + favorite + tag + pinned(AND 语义 — 任一不满足就 False)。

        2026-09-14 v1.7.5 PR #8:filter state 走 self 上的 4 个字段:
        `_filter_text` / `_favorite_only` / `_tag_only` / `_pinned_only`。
        `_filter_text` 空 = 不参与 AND(只过滤元数据);元数据字段 False
        = 不参与 AND。
        """
        if self._favorite_only and not m.is_favorite:
            return False
        if self._tag_only and not m.tags:
            return False
        if self._pinned_only and not m.is_pinned:
            return False
        text = self._filter_text
        if not text:
            return True
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
        # 2026-09-10 v1.7.3:用户元数据 / pin 图标行 — ★ 收藏 / 🏷tags / 📝notes 截断
        # / 📌 pin。与 v1.7.2 metadata 字段配套:v1.7.2 加了字段但没渲染,本版本补
        # 上。用户右键 ★ 后行立即显示 ★(无需 reload)。
        meta_icons = _format_meta_icons(m)
        if meta_icons:
            head += "  " + meta_icons
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
    - media 行 → fillRect 底色(走 palette AlternateBase,主题切换不破皮)
    - 普通行 → QTextDocument 渲 rich text(支持 word wrap)

    2026-09-11 v1.7.5:删 `MEDIA_BG = QColor(232, 240, 248)` 硬编码浅蓝
    → 改走 `option.palette.brush(QPalette.AlternateBase)`(Qt 主题感知,
    暗色主题下自动给 darker shade)。同时支持 `style.qss` `#messageMediaRow`
    selector 自定义(若 QSS 设置 QPalette,override 优先级 QtStyle 决定)。

    2026-09-15 v1.7.5 PR #9 perf:加 `_size_hint_cache` + `_doc_cache` —
    - `_size_hint_cache: dict[(cid, mid, width), QSize]` — sizeHint O(1) 命中,
      避免每帧 QTextDocument 重建(setHtml + setTextWidth + size + idealWidth)
      单 row paint ~5-10ms → 缓存命中 ~0.1ms。
    - `_doc_cache: dict[(cid, mid), QTextDocument]` — paint 命中复用 doc 实例,
      避免 setHtml 重做。LRU 200(队列里 FIFO 淘汰,贴近真实窗口可见行 30-50,
      留 4x 缓冲)。dataChanged 时由调用方主动 clear(详见 `clear_caches`)。
    """

    # PR #9:doc 缓存上限。10K 行 × ~30KB doc ≈ 300MB,LRU 200 ≈ 6MB 可接受。
    _DOC_CACHE_MAX = 200

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # PR #9 perf:sizeHint 缓存 — keyed by (cid, mid, width)。width
        # 变化(窗口 resize)时本 row 自然 miss → 重算 + 缓存。
        self._size_hint_cache: dict[tuple[int, int, int], QSize] = {}
        # PR #9 perf:paint doc 缓存 — keyed by (cid, mid),LRU 200 淘汰。
        # doc 实例在 `paint` 内 reuse,只调 `setTextWidth(rect.width())`
        # 适配当前行宽 — 比重建省一次 setHtml。
        self._doc_cache: dict[tuple[int, int], QTextDocument] = {}
        self._doc_cache_lru: list[tuple[int, int]] = []  # FIFO 队列

    def clear_caches(self) -> None:
        """2026-09-15 v1.7.5 PR #9:数据全表失效(set_messages / reset /
        set_channel_titles / 大批 append)时清两个 cache。普通编辑路径由
        model.dataChanged([FormattedRole]) 自动覆盖 → 不必每条都 clear。
        """
        self._size_hint_cache.clear()
        self._doc_cache.clear()
        self._doc_cache_lru.clear()

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
            # 2026-09-11 v1.7.5:用 palette 替代硬编码 QColor — 暗色主题下自动
            # 变暗,无需手动 if/else。palette 由 Qt theme + style.qss 提供。
            painter.fillRect(option.rect, option.palette.brush(QPalette.AlternateBase))
        text = index.data(MessageListModel.FormattedRole) or ""
        if not text:
            return
        # 2026-09-15 v1.7.5 PR #9 perf:复用缓存 QTextDocument — 命中时省
        # setHtml 一遍(对长消息 ~1-3ms);未命中时新建 + 缓存 + 写 LRU。
        dto = index.data(MessageListModel.DtoRole)
        key = (dto.channel_id, dto.telegram_msg_id) if isinstance(dto, MessageDTO) else None
        doc: QTextDocument | None = None
        if key is not None and key in self._doc_cache:
            doc = self._doc_cache[key]
        else:
            doc = QTextDocument()
            doc.setDefaultFont(option.font)
            doc.setHtml(self._plain_to_html(text))
            if key is not None:
                self._doc_cache[key] = doc
                self._doc_cache_lru.append(key)
                if len(self._doc_cache_lru) > self._DOC_CACHE_MAX:
                    evict = self._doc_cache_lru.pop(0)
                    self._doc_cache.pop(evict, None)
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
        # 2026-09-15 v1.7.5 PR #9 perf:sizeHint 缓存。key = (cid, mid, width)。
        # width 变化 → miss → 重算并覆盖缓存(width 是 cache key 的一部分)。
        dto = index.data(MessageListModel.DtoRole)
        if isinstance(dto, MessageDTO):
            width = option.rect.width() if option.rect.width() > 0 else 280
            cache_key = (dto.channel_id, dto.telegram_msg_id, int(width))
            cached = self._size_hint_cache.get(cache_key)
            if cached is not None:
                return cached
            doc = QTextDocument()
            doc.setDefaultFont(option.font)
            doc.setHtml(self._plain_to_html(text))
            doc.setTextWidth(width)
            size = QSize(int(doc.idealWidth()), int(doc.size().height()) + 8)
            self._size_hint_cache[cache_key] = size
            return size
        # 没 DTO(model 直接给 FormattedRole)→ 走老路径
        doc = QTextDocument()
        doc.setDefaultFont(option.font)
        doc.setHtml(self._plain_to_html(text))
        width = option.rect.width() if option.rect.width() > 0 else 280
        doc.setTextWidth(width)
        # 高度 = 内容 + 上下各 4px padding
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

    MAX_ITEMS = (
        10000  # 2026-09-03 v1.5.4 PR #P1:实验性 bump(原 1000,CHANGELOG v1.5.3 PR #D1 已埋钩子)
    )

    # 用户点击一条消息 → emit MessageDTO 给详情面板
    message_selected = Signal(object)

    # 2026-09-08 v1.7.0:多选 + 右键菜单 + 顶部 toolbar 联动信号
    selection_count_changed = Signal(int)
    selection_messages_changed = Signal(list)  # list[(cid, mid)]
    export_requested = Signal(list)  # 右键「导出」触发
    delete_requested = Signal(list)  # 右键 / toolbar「删除」触发
    mark_read_requested = Signal(list)  # 右键 / toolbar「标记已读」触发
    # 2026-09-09 v1.7.2:用户元数据 — favorite / tags / notes 单条入口。
    # payload (channel_id, telegram_msg_id);右键菜单触发。
    favorite_requested = Signal(int, int, bool)  # cid, mid, new_value
    tags_requested = Signal(int, int)
    notes_requested = Signal(int, int)

    def __init__(self) -> None:
        """初始化 model + delegate + channel_titles + filter + empty overlay。"""
        super().__init__()
        self.setAlternatingRowColors(True)
        self.setUniformItemSizes(False)  # delegate 动态 size
        self.setWordWrap(True)
        # 2026-09-08 v1.7.0:切 ExtendedSelection — Ctrl/Shift 多选开箱。
        # 单一 current 选中(MessageDetail 仍走 message_selected signal 不受影响)。
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        self._model = MessageListModel(self)
        self.setModel(self._model)

        self._delegate = MessageItemDelegate(self)
        # 2026-09-15 v1.7.5 PR #9:model 注入 delegate 引用,`reset()` 时通知
        # 清 doc / sizeHint cache;避免 set_messages 切换频道后 paint 命中
        # 旧消息的 doc。
        self._model.set_delegate(self._delegate)
        self.setItemDelegate(self._delegate)

        # 点击 → 取 DTO → emit
        self.clicked.connect(self._on_clicked)

        # 2026-09-08 v1.7.0:selection 变化 → emit 信号给 toolbar
        sel_model = self.selectionModel()
        if sel_model is not None:
            sel_model.selectionChanged.connect(self._on_selection_changed)

        # 空状态占位(默认显示,首条消息到达自动隐藏)
        # 2026-09-14 v1.7.5 PR #5 (P0-L):三态 overlay — 未订阅 / 搜索无结果 / LIVE 空
        self._empty_overlay = empty_hint(
            icon="💬",
            title="暂无消息",
            hint="先去「频道」页双击订阅一个频道,\n新消息会实时显示在这里。",
            parent=self,
        )
        self._empty_overlay.raise_()
        self._refresh_empty_state()
        # 2026-09-14 v1.7.5 PR #5 (P0-L):默认 state — MainWindow 在登录后
        # 通过 `live_view.set_empty_state(...)` 切到对应 overlay。
        self._empty_state = "no_subscribed"
        # model 行数变化时刷新 overlay
        self._model.rowsInserted.connect(self._refresh_empty_state)
        self._model.rowsRemoved.connect(self._refresh_empty_state)
        self._model.modelReset.connect(self._refresh_empty_state)
        # 2026-09-14 v1.7.5 PR #5 (P0-I):新消息浮条 — 用户在浏览旧消息时,
        # 有新 LIVE 消息到达且不在 scroll top → 显示「N 条新消息 ↓」浮动按钮。
        # 点 → scrollToTop + 隐藏。
        self._new_msg_count: int = 0
        self._floating_btn = QPushButton(self.tr("0 条新消息 ↓"), self)
        self._floating_btn.setObjectName("floatingNewMsgBtn")
        self._floating_btn.setCursor(Qt.PointingHandCursor)
        self._floating_btn.hide()
        self._floating_btn.clicked.connect(self._on_floating_btn_clicked)
        # rowsInserted → 检查 scroll position 决定是否 +1 + 显示
        self._model.rowsInserted.connect(self._on_row_inserted_floating)

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

        2026-09-15 v1.7.5 PR #9:整批替换只 emit 1 次 `modelReset`,避免
        `clear_view() + reset(...)` 双 reset 带来的 2 次 signal + 2 次
        delegate cache 清。
        """
        # 2026-09-14 v1.7.5 PR #5 (P0-I):set_messages 是批量替换,清空浮条计数
        self.clear_new_msg_counter()
        # 2026-09-15 v1.7.5 PR #9:单次 reset(new=[] 也能正确清空;model.reset
        # 已分支处理空列表)。`model.reset` 接受按 list 顺序存储,而 view 是
        # newest-first(top=row 0 是最新),所以 caller 给 messages 是 date ASC
        # (newest-last)→ caller 调 reversed 传入。详细见 _model.reset。
        self._model.reset(list(reversed(messages)) if messages else [])
        # 截断后 apply 现有 filter(set_messages 整批替换,apply 一次省心)
        # 2026-09-14 v1.7.5 PR #8:apply 当前完整 filter state(text + 3 元数据)。
        if (
            self._model._filter_text
            or self._model._favorite_only
            or self._model._tag_only
            or self._model._pinned_only
        ):
            self._model.set_filter(
                self._model._filter_text,
                favorite_only=self._model._favorite_only,
                tag_only=self._model._tag_only,
                pinned_only=self._model._pinned_only,
            )

    def set_channel_titles(self, titles: dict[int, str]) -> None:
        """外部注入频道 id → title 映射 — 委托 model。"""
        self._model.set_channel_titles(titles)

    def set_filter(
        self,
        text: str = "",
        *,
        favorite_only: bool = False,
        tag_only: bool = False,
        pinned_only: bool = False,
    ) -> None:
        """过滤 LIVE 流 — 文本 + 3 元数据维度(2026-09-14 v1.7.5 PR #8)。

        向后兼容:旧调用 `set_filter(text)` 仍能工作(元数据维度默认 False)。
        """
        self._model.set_filter(
            text,
            favorite_only=favorite_only,
            tag_only=tag_only,
            pinned_only=pinned_only,
        )

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

    def refresh_reactions(
        self,
        channel_id: int,
        telegram_msg_id: int,
        reactions: list[ReactionDTO] | None,
    ) -> None:
        """2026-09-11 v1.7.4:TDLib push reactions 变化 → 局部刷新一行。

        详见 `MessageListModel.refresh_reactions`(调用同方法)。
        """
        self._model.refresh_reactions(channel_id, telegram_msg_id, reactions)

    def dto_by_key(self, channel_id: int, telegram_msg_id: int) -> MessageDTO | None:
        """2026-09-11 v1.7.4:按 (cid, mid) 拿当前 DTO 引用 — 给详情面板同步 reactions 用。

        返回 LIVE 模型里那个对象(可变引用) — 详情面板拿到后 view 自己
        `show_message(dto)` 重建;若不存在返回 None(已被截断 / 不在 LIVE 视图)。
        """
        return self._model.dto_by_key(channel_id, telegram_msg_id)

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

    # ---- v1.7.0 多选 + 右键菜单 ----

    def selected_messages(self) -> list[tuple[int, int]]:
        """返 `[(channel_id, telegram_msg_id), ...]` — 按 row 顺序。"""
        out: list[tuple[int, int]] = []
        sel_model = self.selectionModel()
        if sel_model is None:
            return out
        for idx in sel_model.selectedRows():
            dto = self._model.data(idx, MessageListModel.DtoRole)
            if isinstance(dto, MessageDTO):
                out.append((dto.channel_id, dto.telegram_msg_id))
        return out

    def selection_count(self) -> int:
        sel_model = self.selectionModel()
        return len(sel_model.selectedRows()) if sel_model is not None else 0

    def clear_selection(self) -> None:
        sel_model = self.selectionModel()
        if sel_model is not None:
            sel_model.clear()

    def _on_selection_changed(self, *_args: object) -> None:
        """selectionChanged → emit 计数 + 选中消息列表给上层。"""
        cnt = self.selection_count()
        self.selection_count_changed.emit(cnt)
        self.selection_messages_changed.emit(self.selected_messages())

    def contextMenuEvent(self, event) -> None:  # noqa: N802 — Qt API
        """2026-09-08 v1.7.0:右键菜单 — 3 动作(导出 / 删除 / 标记已读)。

        空选区时仍可弹(用于「全选 / 清除」子集场景);0 选菜单项
        `setEnabled(False)`。
        """
        menu = QMenu(self)
        count = self.selection_count()
        sel = self.selected_messages()

        act_export = QAction(self.tr("导出…"), menu)
        act_export.setEnabled(count >= 1)
        act_export.triggered.connect(lambda: self.export_requested.emit(sel))
        menu.addAction(act_export)
        menu.addSeparator()

        act_delete = QAction(self.tr("删除"), menu)
        act_delete.setEnabled(count >= 1)
        act_delete.triggered.connect(lambda: self.delete_requested.emit(sel))
        menu.addAction(act_delete)
        menu.addSeparator()

        act_read = QAction(self.tr("标记已读"), menu)
        act_read.setEnabled(count >= 1)
        act_read.triggered.connect(lambda: self.mark_read_requested.emit(sel))
        menu.addAction(act_read)

        # 2026-09-09 v1.7.2:用户元数据菜单 — 单条才允许。
        menu.addSeparator()
        act_fav = QAction(self.tr("★ 收藏 / 取消收藏"), menu)
        # 元数据操作要求恰好 1 条;多选时禁用,避免歧义。
        act_fav.setEnabled(count == 1)
        if count == 1:
            current = sel[0] if sel else None
            if current is not None:
                cid, mid = current[0], current[1]
                act_fav.triggered.connect(
                    lambda: self.favorite_requested.emit(
                        cid, mid, not getattr(current, "is_favorite", False)
                    )
                )
        menu.addAction(act_fav)

        act_tags = QAction(self.tr("🏷 设置标签…"), menu)
        act_tags.setEnabled(count == 1)
        if count == 1 and sel:
            cid, mid = sel[0][0], sel[0][1]
            act_tags.triggered.connect(lambda: self.tags_requested.emit(cid, mid))
        menu.addAction(act_tags)

        act_notes = QAction(self.tr("📝 设置备注…"), menu)
        act_notes.setEnabled(count == 1)
        if count == 1 and sel:
            cid, mid = sel[0][0], sel[0][1]
            act_notes.triggered.connect(lambda: self.notes_requested.emit(cid, mid))
        menu.addAction(act_notes)

        menu.exec_(event.globalPos())

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
        # 2026-09-14 v1.7.5 PR #5 (P0-I):浮条 resize 时也跟浮动 top-right。
        self._reposition_floating_btn()

    # ---- 2026-09-14 v1.7.5 PR #5:新消息浮条 + 空状态分型 ----

    def _on_row_inserted_floating(self, parent, first: int, last: int) -> None:
        """rowsInserted handler:若 scroll 不在最顶 → 累加未读数 + 显示浮条。

        `first == last == 0` 是 LIVE 单条到达场景。set_messages 是
        modelReset(emit modelReset,不 emit rowsInserted),所以本 handler
        不会被它触发 — 不需要在这里清零 counter。
        """
        # 在最顶 → 不算「错过的新消息」(用户在看最新)
        if self.verticalScrollBar().value() <= 0:
            return
        added = last - first + 1
        self._new_msg_count += added
        self._floating_btn.setText(self.tr("%d 条新消息 ↓").format(self._new_msg_count))
        self._floating_btn.show()
        self._reposition_floating_btn()

    def _reposition_floating_btn(self) -> None:
        """浮条贴 top-right,留 16px padding。"""
        if not self._floating_btn.isVisible():
            return
        size = self._floating_btn.sizeHint()
        x = max(0, self.width() - size.width() - 16)
        self._floating_btn.setGeometry(x, 16, size.width(), size.height())
        self._floating_btn.raise_()

    def _on_floating_btn_clicked(self) -> None:
        """点浮条 → scrollToTop + 清计数 + 隐藏。"""
        # newest-first 列表,top row = 最新一条
        self.scrollToTop()
        self._new_msg_count = 0
        self._floating_btn.hide()

    def set_empty_state(self, state: str) -> None:
        """2026-09-14 v1.7.5 PR #5 (P0-L):切换空状态文案。

        Args:
            state: "no_subscribed" / "searching" / "live_empty"。
        """
        self._empty_state = state
        if state == "no_subscribed":
            self._empty_overlay_title(
                "💬",
                self.tr("未订阅频道"),
                self.tr("先去「频道」页双击订阅一个频道,\n新消息会实时显示在这里。"),
            )
        elif state == "searching":
            self._empty_overlay_title(
                "🔍", self.tr("无匹配结果"), self.tr("试试更换关键词或日期范围。")
            )
        elif state == "live_empty":
            self._empty_overlay_title("💬", self.tr("暂无消息"), self.tr("新消息会实时显示。"))

    def _empty_overlay_title(self, icon: str, title: str, hint: str) -> None:
        """直接改 overlay 内 icon / title / hint label 文字(不重建 widget)。"""
        # empty_hint 返回 widget 含 3 个子 widget:icon QLabel + title QLabel + hint QLabel
        # 通过 findChildren 拿 label 后改 text。
        from PySide6.QtWidgets import QLabel

        labels = self._empty_overlay.findChildren(QLabel)
        # labels 顺序:[icon, title, hint](empty_hint 实现保证)
        if len(labels) >= 1:
            labels[0].setText(icon)
        if len(labels) >= 2:
            labels[1].setText(title)
        if len(labels) >= 3:
            labels[2].setText(hint)
        self._empty_overlay.raise_()

    def clear_new_msg_counter(self) -> None:
        """VM 搜索 / set_messages reset 时清浮条计数。"""
        self._new_msg_count = 0
        self._floating_btn.hide()
