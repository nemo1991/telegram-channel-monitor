# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""MessageListModel — 5 mixin MRO 链 — 2026-10-16 plan-batch7 目标 2。

原 `MessageListModel` 单类 399 LOC 拆 5 mixin:
- `_MessageListModelBase`(MRO 末):role enum + rowCount + data + flags +
  `__init__`(7 个字段单点 init 收敛)
- `StorageMixin`:DTO 路径查询(dto_by_key / row_of_key / row_values)
- `MutationMixin`:DTO 路径写 + cache invalidation + Qt model signal
  (append / remove / reset / set_delegate / replace / update_media /
  refresh_reactions)
- `FilterMixin`:4 filter state 字段 + AND 语义(set_filter /
  set_channel_titles / _matches)
- `FormatMixin`:`_format` + `_format_meta_icons` helper

MRO 顺序:`MessageListModel(Format, Filter, Mutation, Storage, _Base)` —
4 mixin 在前,Base 在末(super().__init__(parent) 落 QAbstractListModel)。

公开 API 锁 1:1:11 method + 5 role 全保留;concrete class body 仅 1 行
MRO 声明 + 1 行 `MAX_ITEMS = 10000`(语义从 MessageView 搬来)。

Qt model 必须 `QAbstractListModel` 子类(beginInsertRows / dataChanged /
modelReset 走 Shiboken 元类绑定),composition delegate 模式不可用
(plan-batch2-3 tdlib_client 经验证)— mixin 是唯一可行拆法。这是项目
首次引入 mixin 模式,Phase 1 探查确认 Python C3 + Shiboken 兼容。
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
)

if TYPE_CHECKING:
    from tgmonitor.ui.widgets.message_item_delegate import MessageItemDelegate

from tgmonitor.core.dto import MessageDTO
from tgmonitor.ui.widgets.message_list_model_filter import FilterMixin
from tgmonitor.ui.widgets.message_list_model_format import FormatMixin
from tgmonitor.ui.widgets.message_list_model_mutation import MutationMixin
from tgmonitor.ui.widgets.message_list_model_storage import StorageMixin


class _MessageListModelBase(QAbstractListModel):
    """Base mixin:role enum + Qt model 接口(rowCount / data / flags)+ 字段 init。

    2026-10-16 plan-batch7 目标 2:把所有 7 个字段(原 L120-143 __init__ 内)
    集中在此 init,4 mixin 共享。mixin 类体不放 `__init__`(避免 5 个 super
    样板 + 字段归属混乱)。

    字段分组(Base init 单点设):
    - 存储层:`_items` / `_index_of` / `_format_cache` / `_channel_titles`
    - 过滤层:`_filter_text` / `_favorite_only` / `_tag_only` / `_pinned_only`
    - mutation 层:`_delegate`
    """

    # 2026-09-02 v1.5.3 PR #D1:`QListWidgetItem` 内部存储 → 真实 Qt model,
    # delegate 按需 paint。**对外通过 role 协议暴露数据**:`data(idx, role)`
    # 按 role 返 DTO / msg_id / hidden flag / formatted 富文本 / has_media。
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
        # 2026-09-14 v1.7.5 PR #8:3 个用户元数据过滤维度(AND 语义)。
        self._filter_text: str = ""
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
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),
    ) -> int:
        if parent.isValid():
            return 0
        return len(self._items)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.DisplayRole,
    ):
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

    def flags(
        self,
        index: QModelIndex | QPersistentModelIndex,
    ) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable


class MessageListModel(
    FormatMixin,
    FilterMixin,
    MutationMixin,
    StorageMixin,
    _MessageListModelBase,
):
    """5 mixin MRO 链。concrete class 仅 1 行 MRO 声明 + `MAX_ITEMS` 类属性,无 body method。

    公开 API 锁 1:1(原 MessageListModel 11 method + 5 role 全保留):
    - mutator:append / reset / set_delegate / remove_by_key /
      replace_message / update_media_status / refresh_reactions
    - filter:set_filter / set_channel_titles
    - storage:dto_by_key / row_of_key / row_values
    - format:`_format`(内部,paint 热路径走 cache)
    - 内部 helper:`_matches` / `_truncate_tail` / `_truncate_tail_inplace`

    Qt model 必需信号(dataChanged / modelReset / beginInsertRows 等)由
    QAbstractListModel 提供,通过 MRO 链 `self.X` 解析(Shiboken 实例方法
    绑定,不依赖 mixin 归属)。

    2026-10-16:`MAX_ITEMS = 10000` 从 MessageView 搬来(语义上属 model
    容量上限,不是 view 属性)。`MessageView.MAX_ITEMS` 加 alias 保
    test_message_view.py L575 等老引用零改动。
    """

    # 2026-09-03 v1.5.4 PR #P1:实验性 bump(原 1000,CHANGELOG v1.5.3 PR #D1 已埋钩子)
    # 2026-10-16 plan-batch7 目标 2:语义搬到 MessageListModel(主类),
    # MessageView 端 `MAX_ITEMS = MessageListModel.MAX_ITEMS` alias 兼容 test。
    MAX_ITEMS = 10000
