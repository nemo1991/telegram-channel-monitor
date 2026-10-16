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

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 2026-10-15 plan-batch7-C-α:循环 import 防护 — controller
    # 文件顶部 `from message_view import MessageListModel` 已触发本 module
    # 加载,运行时 view 不直接 import controller(只 type annotation),
    # MainWindow / fixture 显式 `from tgmonitor.ui.controllers.live_actions_controller
    # import LiveActionsController` 后通过 `view._live_actions = ctl(view)` 注入。
    from tgmonitor.ui.controllers.live_actions_controller import LiveActionsController

from PySide6.QtCore import (
    QModelIndex,
    Signal,
)

# 2026-10-15 plan-batch7-A:QPalette / QTextDocument / QStyledItemDelegate /
# QStyleOptionViewItem 4 个仅 delegate 用,delegate 已搬到 message_item_delegate.py
# 独立 module,主文件不再 import。
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListView,
)

from tgmonitor.core.dto import MessageDTO

# 2026-10-15 plan-batch7-A:MessageItemDelegate 已在 message_item_delegate.py
# 独立定义,主类用 `from tgmonitor.ui.widgets.message_item_delegate import
# MessageItemDelegate` 走 re-export(见文件末尾),import 顺序保留空一格便于
# 静态检查。
# 2026-10-15 plan-batch7-B:空 overlay + 浮条已搬到 message_empty_overlay.py
# 独立 widget,主类不再 import empty_hint / QPushButton / QLabel(仅 widget 用)。
# 2026-10-16 plan-batch7 目标 2:MessageListModel 拆 5 mixin 落到
# `message_list_model_*.py` 5 个文件,本文件 re-export 保 backward compat;
# `format_reactions_short` / `deque` / `UTC` 3 个仅 model 用,view 端不再 import。
# ============================================================
# 2026-10-16 plan-batch7 目标 2:MessageListModel 拆 5 mixin
# (Base + Storage + Mutation + Filter + Format),落到
# `message_list_model.py` 主类 + 4 mixin 各自独立文件。本文件 re-export
# 保 `from tgmonitor.ui.widgets.message_view import MessageListModel`
# 既有 import 路径零改动。
# ============================================================
from tgmonitor.ui.widgets.message_list_model import MessageListModel  # re-export
from tgmonitor.ui.widgets.message_list_model_format import (  # re-export  # noqa: F401
    _format_meta_icons,
)

# 2026-10-16 plan-batch7 目标 3 commit B:14 thin delegate 抽到
# `message_view_api_mixin.py`;主类改多继承 `MessageViewAPIMixin, QListView`。
# 14 method:append / set_messages / set_channel_titles / set_filter /
# remove_row / clear_view / replace_message / update_media_status /
# refresh_reactions / dto_by_key / row_of_key / count / _format。
# set_messages 内部 14 LOC filter re-apply 块抽到 mixin 文件
# module-level `_reapply_filter_if_active(model)`(mixin 不触碰 model 私有字段)。
from tgmonitor.ui.widgets.message_view_api_mixin import MessageViewAPIMixin

# ============================================================
# MessageView — QListView 子类
# ============================================================


class MessageView(MessageViewAPIMixin, QListView):
    """实时消息流 — QListView + MessageListModel + MessageItemDelegate。

    2026-09-02 v1.5.3 PR #D1:从 QListWidget 子类改为 QListView + 自定义
    model + delegate。公开 API 全部保留(append / set_messages /
    set_filter / set_channel_titles / remove_row / clear_view /
    replace_message / update_media_status / message_selected signal /
    count / MAX_ITEMS / _format),内部 model + delegate 换皮。
    """

    # 2026-09-03 v1.5.4 PR #P1:实验性 bump(原 1000,CHANGELOG v1.5.3 PR #D1 已埋钩子)
    # 2026-10-16 plan-batch7 目标 2:语义搬到 MessageListModel(主类,容量上限属 model)。
    # view 端加 alias 保 test_message_view.py L575 等老引用零改动。
    MAX_ITEMS = MessageListModel.MAX_ITEMS

    # 用户点击一条消息 → emit MessageDTO 给详情面板(message_selected
    # 是 `currentRowChanged` signal,不在 plan-batch7-C-α 8 Signal 范围)
    message_selected = Signal(object)

    # 2026-10-15 plan-batch7-C-α:8 Signal(selection_count_changed /
    # selection_messages_changed / export_requested / delete_requested /
    # mark_read_requested / favorite_requested / tags_requested /
    # notes_requested)整体搬到 `LiveActionsController(QObject)`,
    # 公开 API 锁由 controller 持有。view contextMenuEvent 转发到
    # controller,持 `_live_actions: LiveActionsController | None` 引用
    # (MainWindow 启动时注入,fixture / 测试也可显式 attach)。

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

        # 2026-10-15 plan-batch7-C-α:`selectionChanged` → 信号 emit
        # 整体搬到 LiveActionsController,view 不再监听
        # selectionModel().selectionChanged(由 controller 自己接)。

        # 2026-10-15 plan-batch7-C-α:LiveActionsController 由 MainWindow
        # 启动时注入,默认 None。fixture / 测试可显式 attach
        # (`view._live_actions = LiveActionsController(view)`)。
        self._live_actions: LiveActionsController | None = None

        # 2026-10-15 plan-batch7-B:空 overlay + 浮条已抽到 MessageEmptyOverlay
        # widget,主类通过 setter API(set_state / set_messages_count /
        # update_for_row_count / reposition)通信,不再直接持 widget 内部
        # 状态(单一 owner 在 widget)。
        # 2026-09-14 v1.7.5 PR #5 (P0-L + P0-I):三态 overlay + 新消息浮条 —
        # 未订阅 / 搜索无结果 / LIVE 空 + 「N 条新消息 ↓」浮动按钮。
        self._empty_overlay_widget = MessageEmptyOverlay(parent=self)
        # 2026-10-15 plan-batch7-B:widget 自身 setGeometry 覆盖整个 view —
        # MessageEmptyOverlay 内 empty_hint 默认 sizeHint 在 size=0 parent
        # 里不显示(QWidget 不渲染 size=0 子 widget),必须让 widget 自身
        # 至少与 view 同大,`reposition(w, h)` 计算的居中位置才生效。
        # 原 message_view 的 empty_hint 显式 `setGeometry(x, y, hint_w,
        # hint_h)` 给 overlay 本体,不依赖 parent 渲染;widget 嵌套后
        # empty_hint 改在 widget 内,需 widget 自身先有 geometry。
        self._empty_overlay_widget.setGeometry(self.rect())
        # 2026-10-15 plan-batch7-B:backward compat — 测试 fixture 直接读
        # `view._empty_overlay.isHidden()`,代理到 widget 内部 overlay
        # 引用,避免 4 个 test 改路径(test_empty_overlay_shown_when_no_messages
        # / test_empty_overlay_hidden_after_first_append /
        # test_empty_overlay_reappears_after_clear / test_message_view_empty
        # 视觉回归黄金图比对)。
        self._empty_overlay = self._empty_overlay_widget._empty_overlay
        self._empty_overlay_widget.floating_clicked.connect(self.scrollToTop)
        # 2026-10-15 plan-batch7-B:widget 自身 raise_ 到 view 顶部 Z-order
        # — widget 内部 empty_hint raise_ 只到 widget,widget 自身需主动
        # raise_ 到 view(parent)才能让 empty_hint 渲染在列表之上。
        # 原 message_view L566 `_empty_overlay.raise_()` 的等价行为。
        self._empty_overlay_widget.raise_()
        # 默认 state — MainWindow 登录后通过 `live_view.set_empty_state(...)`
        # 切到对应 overlay(原 `_empty_state` 字段由 widget 自己持)。
        # model 行数变化时刷新 overlay + 浮条
        self._model.rowsInserted.connect(self._on_row_count_changed)
        self._model.rowsRemoved.connect(self._on_row_count_changed)
        self._model.modelReset.connect(self._on_row_count_changed)
        # rowsInserted → 检查 scroll position 决定是否 +1 + 显示(浮条逻辑)
        self._model.rowsInserted.connect(self._on_row_inserted_floating)

    # ---- 公开 API(14 thin delegate 已抽到 MessageViewAPIMixin,mixin 在多继承首位) ----
    # 见 widgets/message_view_api_mixin.py:append / set_messages / set_channel_titles
    # / set_filter / remove_row / clear_view / replace_message /
    # update_media_status / refresh_reactions / dto_by_key / row_of_key /
    # count / _format。

    def current_message(self) -> MessageDTO | None:
        """2026-09-02 v1.5.3 PR #D1:`main_window._copy_current_message_text` 用。

        替代旧 `currentItem().data(Qt.UserRole)`。
        """
        idx = self.currentIndex()
        if not idx.isValid():
            return None
        return self._model.data(idx, MessageListModel.DtoRole)

    # ---- 内部 ----

    def _on_clicked(self, index: QModelIndex) -> None:
        dto = self._model.data(index, MessageListModel.DtoRole)
        if isinstance(dto, MessageDTO):
            self.message_selected.emit(dto)

    # ---- v1.7.0 多选 + 右键菜单 ----

    def selected_messages(self) -> list[tuple[int, int]]:
        """返 `[(channel_id, telegram_msg_id), ...]` — 按 row 顺序。

        2026-10-16 plan-batch7 目标 3 commit A:委托 `LiveActionsController.selected_messages` —
        无 controller 时本地 fallback(fixture / 单测未 attach 路径)。
        """
        if self._live_actions is not None:
            return self._live_actions.selected_messages()
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
        """2026-10-16 plan-batch7 目标 3 commit A:委托 `LiveActionsController.selection_count` —
        无 controller 时本地 fallback。
        """
        if self._live_actions is not None:
            return self._live_actions.selection_count()
        sel_model = self.selectionModel()
        return len(sel_model.selectedRows()) if sel_model is not None else 0

    def clear_selection(self) -> None:
        """2026-10-16 plan-batch7 目标 3 commit A:委托 `LiveActionsController.clear_selection` —
        无 controller 时本地 fallback。
        """
        if self._live_actions is not None:
            self._live_actions.clear_selection()
            return
        sel_model = self.selectionModel()
        if sel_model is not None:
            sel_model.clear()

    # ---- 2026-10-15 plan-batch7-C-α:LiveActionsController 注入 ----

    @property
    def live_actions(self):
        """2026-10-15 plan-batch7-C-α:公开 getter 给 LiveActionsController
        (QObject)— 持 8 Signal + 右键菜单 + 选区广播。MainWindow 启动时
        注入,默认 None。测试 fixture 可显式 attach。
        """
        return self._live_actions

    def contextMenuEvent(self, event) -> None:  # noqa: N802 — Qt API
        """2026-09-08 v1.7.0:右键菜单 — thin delegate 给 LiveActionsController。

        2026-10-15 plan-batch7-C-α:右键菜单 6 动作(导出 / 删除 / 标记已读 /
        收藏 / 标签 / 备注)构造逻辑整体搬到 controller,view 仅转发
        event.globalPos() 给 controller.show_context_menu。
        `_live_actions` 未注入时(测试 fixture 默认)no-op。
        """
        if self._live_actions is None:
            return
        self._live_actions.show_context_menu(event.globalPos())

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt override
        """窗口尺寸变 → widget 覆盖 view 整个区域 + overlay/浮条重定位。"""
        super().resizeEvent(event)
        # 2026-10-15 plan-batch7-B:widget 自身 size 跟 view 同步(否则 size=0
        # widget 内 empty_hint 不渲染),reposition 内计算居中位置。
        self._empty_overlay_widget.setGeometry(self.rect())
        self._empty_overlay_widget.reposition(self.width(), self.height())

    # ---- 2026-10-15 plan-batch7-B:thin delegate 给 widget ----

    def _on_row_count_changed(self, *_args) -> None:
        """rowsInserted/Removed/Reset 触发:count 变 → widget overlay 显隐。"""
        self._empty_overlay_widget.update_for_row_count(self.count())

    def _on_row_inserted_floating(self, parent, first: int, last: int) -> None:
        """rowsInserted handler:若 scroll 不在最顶 → 累加未读数 + 显示浮条。

        `first == last == 0` 是 LIVE 单条到达场景。set_messages 是
        modelReset(emit modelReset,不 emit rowsInserted),所以本 handler
        不会被它触发 — 不需要在这里清零 counter(由 `clear_new_msg_counter`
        显式调)。
        """
        # 在最顶 → 不算「错过的新消息」(用户在看最新)
        if self.verticalScrollBar().value() <= 0:
            return
        added = last - first + 1
        # 2026-10-15 plan-batch7-B:counter 状态搬到 widget,主类调 setter
        cur = self._empty_overlay_widget.messages_count()
        self._empty_overlay_widget.set_messages_count(cur + added)
        self._empty_overlay_widget.show_floating_btn()

    def set_empty_state(self, state: str) -> None:
        """2026-09-14 v1.7.5 PR #5 (P0-L):切换空状态文案 — thin delegate。

        Args:
            state: "no_subscribed" / "searching" / "live_empty"。
        """
        self._empty_overlay_widget.set_state(state)

    def clear_new_msg_counter(self) -> None:
        """VM 搜索 / set_messages reset 时清浮条计数 — thin delegate。"""
        self._empty_overlay_widget.reset_counter()


# 2026-10-15 plan-batch7-A:re-export MessageItemDelegate — 旧
# `from tgmonitor.ui.widgets.message_view import MessageItemDelegate`
# 测试 / 调用面零改动(class 已搬到 message_item_delegate.py)。
# 2026-10-15 plan-batch7-B:re-export MessageEmptyOverlay — 旧 import 路径兼容
from tgmonitor.ui.widgets.message_empty_overlay import MessageEmptyOverlay  # noqa: E402,F401
from tgmonitor.ui.widgets.message_item_delegate import MessageItemDelegate  # noqa: E402,F401
