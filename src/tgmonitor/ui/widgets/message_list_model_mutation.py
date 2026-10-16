# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""MessageListModel mutation mixin — 2026-10-16 plan-batch7 目标 2。

`MutationMixin` 持 DTO 路径写 + cache invalidation + Qt model signal。

8 个 method(每个负责:1) 失效 `_format_cache`  2) emit Qt signal
beginInsertRows / beginRemoveRows / beginResetModel / dataChanged):
- append / _truncate_tail / remove_by_key / reset / set_delegate /
  _truncate_tail_inplace / replace_message / update_media_status /
  refresh_reactions

字段由 `_MessageListModelBase.__init__` 单点 init 收敛;本 mixin 不写
`__init__`。MRO:`MessageListModel(Format, Filter, Mutation, Storage, _Base)`。
"""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QModelIndex

from tgmonitor.core.dto import MediaDTO, MessageDTO, ReactionDTO


class MutationMixin:
    """DTO 路径写 + cache invalidation + Qt model signal。

    每个 mutation 头部契约:
      1) 失效 `_format_cache`(`_format_cache.pop(key, None)` 或 `.clear()`)
      2) emit 适当 Qt signal(`dataChanged` / `beginInsertRows` + `endInsertRows`
         / `beginRemoveRows` + `endRemoveRows` / `beginResetModel` + `endResetModel`)
      3) 通知 delegate cache 全清(`_delegate.clear_caches()`,仅 reset 路径)
    """

    def append(self, m: MessageDTO) -> None:
        """实时追加一条 — 已存在替换,否则插入头部(newest-first)。

        2026-09-15 v1.7.5 PR #9 perf:`deque.appendleft` 替 `list.insert(0)` 拿到
        O(1) head-insert(原 list.insert(0) 是 O(N) Python element shift)。
        `_index_of` bump 仍是 O(N)(所有现有 row 索引 +1),但去掉了 `_row_to_key`
        O(N log N) sort + O(N) shift — 单条 append 总开销从 O(N log N) 降到 O(N)。
        """
        key = (m.channel_id, m.telegram_msg_id)
        if key in self._index_of:  # type: ignore[attr-defined]
            # 已存在 — 文本可能更新(edit),替换那一行
            row = self._index_of[key]  # type: ignore[attr-defined]
            self._items[row] = m  # type: ignore[attr-defined]  # deque 支持 __setitem__
            self._format_cache.pop(key, None)  # type: ignore[attr-defined]  # PR #9:失效缓存
            idx = self.index(row, 0)
            self.dataChanged.emit(
                idx,
                idx,
                [self.DtoRole, self.FormattedRole, self.HiddenRole, self.HasMediaRole],  # type: ignore[attr-defined]
            )
            return
        # 插入头部 — O(1) via deque.appendleft
        self.beginInsertRows(QModelIndex(), 0, 0)
        self._items.appendleft(m)  # type: ignore[attr-defined]
        # _index_of 全部 +1(现有 row 都向后挪 1)— O(N) bump
        for k in self._index_of:  # type: ignore[attr-defined]
            self._index_of[k] += 1  # type: ignore[attr-defined]
        self._index_of[key] = 0  # type: ignore[attr-defined]
        self.endInsertRows()
        # MAX_ITEMS 截断(尾部删,走 O(1) deque.pop())
        while len(self._items) > self.MAX_ITEMS:  # type: ignore[attr-defined]
            self._truncate_tail()

    def _truncate_tail(self) -> None:
        """删尾部一行(物理位置 len-1 = 最旧)— 不需要 _index_of shift。

        newest-first 布局:`_items[0]` = newest(刚 appendleft 进来的),
        `_items[len-1]` = oldest。MAX_ITEMS 超限要砍最旧 = 删 row len-1。
        删最末 row 不影响其他 row 的 index(`_index_of` 存的值不变)。
        """
        if not self._items:  # type: ignore[attr-defined]
            return
        last = len(self._items) - 1  # type: ignore[attr-defined]
        self.beginRemoveRows(QModelIndex(), last, last)
        # O(1) 拿 key:最旧的在 `_items[last]`(deque[-1] 也是 O(1))。
        oldest = self._items[last]  # type: ignore[attr-defined]
        removed_key = (oldest.channel_id, oldest.telegram_msg_id)
        del self._index_of[removed_key]  # type: ignore[attr-defined]
        self._format_cache.pop(removed_key, None)  # type: ignore[attr-defined]  # PR #9:失效缓存
        self._items.pop()  # type: ignore[attr-defined]  # O(1) deque tail pop — 删最末 row
        self.endRemoveRows()

    def remove_by_key(self, channel_id: int, telegram_msg_id: int) -> None:
        """按 (channel_id, telegram_msg_id) 删一行 — 找不到 idempotent。

        2026-09-15 PR #9:deque 不支持 `del d[i]`,改用 slice rebuild。仍是 O(N),
        但 remove_by_key 不是热路径(MessageDeleted 频率低)。
        """
        key = (channel_id, telegram_msg_id)
        row = self._index_of.pop(key, None)  # type: ignore[attr-defined]
        if row is None:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        # 重建 deque 跳过 `row` 位 — O(N)
        self._items = deque(m for i, m in enumerate(self._items) if i != row)  # type: ignore[attr-defined]
        self._format_cache.pop(key, None)  # type: ignore[attr-defined]  # PR #9:失效缓存
        # row > row 的 entry -1
        for k in self._index_of:  # type: ignore[attr-defined]
            if self._index_of[k] > row:  # type: ignore[attr-defined]
                self._index_of[k] -= 1  # type: ignore[attr-defined]
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
        self._items = deque(messages)  # type: ignore[attr-defined]
        self._index_of = {  # type: ignore[attr-defined]
            (m.channel_id, m.telegram_msg_id): i
            for i, m in enumerate(self._items)  # type: ignore[attr-defined]
        }
        self._format_cache.clear()  # type: ignore[attr-defined]  # PR #9:cache 全清(消息集合可能全换)
        # PR #9:delegate cache 全清 — 旧 (cid, mid) 文档 + sizeHint 都失效。
        if self._delegate is not None:  # type: ignore[attr-defined]
            self._delegate.clear_caches()  # type: ignore[attr-defined]
        # MAX_ITEMS 截断(尾部删,不走 beginRemoveRows 因为已在 resetModel 中)
        while len(self._items) > self.MAX_ITEMS:  # type: ignore[attr-defined]
            self._truncate_tail_inplace()
        self.endResetModel()

    def set_delegate(self, delegate) -> None:
        """PR #9:注入 delegate 引用 — `reset()` 时通知其清 cache。

        `MessageView.__init__` 里 model 与 delegate 都建好后调用一次。
        单测里没 delegate 时可保持 None,不影响行为。

        Args:
            delegate: `MessageItemDelegate | None` — type annotation 用
                TYPE_CHECKING import 避免循环依赖(主类文件 `message_list_model.py`
                顶部已 `if TYPE_CHECKING: from ... import MessageItemDelegate`)。
        """
        self._delegate = delegate  # type: ignore[attr-defined]

    def _truncate_tail_inplace(self) -> None:
        """reset 中用 — 不走 beginRemoveRows/endRemoveRows(已在 resetModel 中)。

        PR #9:对应 `_truncate_tail`,但省略 Qt signal。`_items.pop()` 删最旧。
        """
        if not self._items:  # type: ignore[attr-defined]
            return
        oldest = self._items[len(self._items) - 1]  # type: ignore[attr-defined]
        removed_key = (oldest.channel_id, oldest.telegram_msg_id)
        del self._index_of[removed_key]  # type: ignore[attr-defined]
        self._format_cache.pop(removed_key, None)  # type: ignore[attr-defined]
        self._items.pop()  # type: ignore[attr-defined]  # O(1) deque tail pop — 删最旧

    def replace_message(self, msg: MessageDTO) -> None:
        """编辑事件:按 key 找 row,重 format + 重检 filter。"""
        key = (msg.channel_id, msg.telegram_msg_id)
        row = self._index_of.get(key)  # type: ignore[attr-defined]
        if row is None:
            # 罕见:编辑事件先于 new message 到达 — 当新增处理
            self.append(msg)
            return
        self._items[row] = msg  # type: ignore[attr-defined]
        self._format_cache.pop(key, None)  # type: ignore[attr-defined]  # PR #9:失效缓存
        idx = self.index(row, 0)
        self.dataChanged.emit(
            idx,
            idx,
            [self.DtoRole, self.FormattedRole, self.HiddenRole, self.HasMediaRole],  # type: ignore[attr-defined]
        )

    def update_media_status(self, channel_id: int, telegram_msg_id: int, media: MediaDTO) -> None:
        """异步下载结束回调:更新 DTO.media + 重 format。"""
        key = (channel_id, telegram_msg_id)
        row = self._index_of.get(key)  # type: ignore[attr-defined]
        if row is None:
            return
        dto = self._items[row]  # type: ignore[attr-defined]
        for i, med in enumerate(dto.media):
            if med is media or (
                media.telegram_file_id and med.telegram_file_id == media.telegram_file_id
            ):
                dto.media[i] = media
                break
        self._format_cache.pop(key, None)  # type: ignore[attr-defined]  # PR #9:失效缓存
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [self.DtoRole, self.FormattedRole])  # type: ignore[attr-defined]

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
        row = self._index_of.get((channel_id, telegram_msg_id))  # type: ignore[attr-defined]
        if row is None:
            return
        # MessageDTO 是 mutable dataclass,直接赋值即可。
        self._items[row].reactions = list(reactions) if reactions else None  # type: ignore[attr-defined]
        self._format_cache.pop((channel_id, telegram_msg_id), None)  # type: ignore[attr-defined]  # PR #9:失效缓存
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [self.DtoRole, self.FormattedRole])  # type: ignore[attr-defined]
