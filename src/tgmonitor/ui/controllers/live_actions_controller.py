"""LiveActionsController — 8 Signal + 右键菜单 + 选区广播 controller。

2026-10-15 plan-batch7-C-α:从 `message_view.py` L519-528 (8 Signal) +
L731-756 (selected_messages/selection_count/clear_selection/
_on_selection_changed) + L758-813 (contextMenuEvent) 整体抽到
独立 QObject controller。view 不再持有这 8 Signal 与右键菜单 / 选区
广播逻辑,持可选 `_live_actions` 引用,contextMenuEvent 转发到
controller 走 thin delegate 公开 API 锁 1:1。

公开契约(QObject signal + method):
  - 8 Signal:
    - selection_count_changed = Signal(int)
    - selection_messages_changed = Signal(list)
    - export_requested = Signal(list)
    - delete_requested = Signal(list)
    - mark_read_requested = Signal(list)
    - favorite_requested = Signal(int, int, bool)
    - tags_requested = Signal(int, int)
    - notes_requested = Signal(int, int)
  - 公开 method(右键菜单 + 选区 API):
    - show_context_menu(global_pos: QPoint)
    - selection_count() -> int
    - selected_messages() -> list[tuple[int, int]]
    - clear_selection()

设计:
- LiveActionsController(QObject) 持 view:MessageView 引用(单 forward dep)
- MainWindow 启动时:
    ctx.live_actions = LiveActionsController(ctx.live_view)
    ctx.live_view._live_actions = ctx.live_actions
- view contextMenuEvent → controller.show_context_menu(event.globalPos())
- controller 内部接 `view.selectionModel().selectionChanged` 触发
  _on_selection_changed → emit selection_count_changed /
  selection_messages_changed
- build_context_menu() 构造 6 action(导出 / 删除 / 标记已读 / 收藏 /
  标签 / 备注),count==1 时元数据菜单项 enable
- 公开 API 锁 1:1 保 backward compat:14 个 method 不动,
  `message_selected` 仍是 `currentRowChanged` signal(不在 8 signal 内)
- 4 controller + assembler wire 表全改:
  ctx.live_view.X → ctx.live_actions.X (8 signal 引用)
"""

from __future__ import annotations

# 2026-10-15 plan-batch7-C-α:与 ui/ 文件惯例一致,屏蔽 PySide6 stub 噪声。
# mypy: disable-error-code="attr-defined"
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu

from tgmonitor.core.dto import MessageDTO
from tgmonitor.ui.widgets.message_view import MessageListModel, MessageView


class LiveActionsController(QObject):
    """2026-09-08 v1.7.0:多选 + 右键菜单 + 顶部 toolbar 联动信号。

    2026-10-15 plan-batch7-C-α:从 MessageView 抽到独立 QObject controller,
    持 view 单 forward dep,8 Signal 全 move 过来。
    """

    # ---- 8 Signal(从 MessageView L519-528 整体搬迁) ----

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

    def __init__(self, view: MessageView, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # 单 forward dep:view — controller 不知道 ctx,通过 view 单向
        # 通信。MainWindow 启动时同时设 ctx.live_actions 字段。
        self._view = view
        # 内部接 selectionChanged → 触发 _on_selection_changed → emit
        sel_model = view.selectionModel()
        if sel_model is not None:
            sel_model.selectionChanged.connect(self._on_selection_changed)

    # ---- 选区 API(从 MessageView L731-750 搬迁) ----

    def selected_messages(self) -> list[tuple[int, int]]:
        """返 `[(channel_id, telegram_msg_id), ...]` — 按 row 顺序。"""
        out: list[tuple[int, int]] = []
        sel_model = self._view.selectionModel()
        if sel_model is None:
            return out
        for idx in sel_model.selectedRows():
            dto = self._view._model.data(idx, MessageListModel.DtoRole)
            if isinstance(dto, MessageDTO):
                out.append((dto.channel_id, dto.telegram_msg_id))
        return out

    def selection_count(self) -> int:
        sel_model = self._view.selectionModel()
        return len(sel_model.selectedRows()) if sel_model is not None else 0

    def clear_selection(self) -> None:
        sel_model = self._view.selectionModel()
        if sel_model is not None:
            sel_model.clear()

    # ---- 内部 _on_selection_changed(从 MessageView L752-756 搬迁) ----

    def _on_selection_changed(self, *_args: object) -> None:
        """selectionChanged → emit 计数 + 选中消息列表给上层。"""
        cnt = self.selection_count()
        self.selection_count_changed.emit(cnt)
        self.selection_messages_changed.emit(self.selected_messages())

    # ---- 右键菜单(从 MessageView L758-813 整体搬迁) ----

    def show_context_menu(self, global_pos) -> None:
        """2026-09-08 v1.7.0:右键菜单 — 6 动作(导出 / 删除 / 标记已读 /
        收藏 / 标签 / 备注)。

        空选区时仍可弹(用于「全选 / 清除」子集场景);0 选菜单项
        `setEnabled(False)`;count != 1 时元数据菜单项 disabled。
        """
        view = self._view
        menu = QMenu(view)
        count = self.selection_count()
        sel = self.selected_messages()

        # 导出
        act_export = QAction(view.tr("导出…"), menu)
        act_export.setEnabled(count >= 1)
        act_export.triggered.connect(lambda: self.export_requested.emit(sel))
        menu.addAction(act_export)
        menu.addSeparator()

        # 删除
        act_delete = QAction(view.tr("删除"), menu)
        act_delete.setEnabled(count >= 1)
        act_delete.triggered.connect(lambda: self.delete_requested.emit(sel))
        menu.addAction(act_delete)
        menu.addSeparator()

        # 标记已读
        act_read = QAction(view.tr("标记已读"), menu)
        act_read.setEnabled(count >= 1)
        act_read.triggered.connect(lambda: self.mark_read_requested.emit(sel))
        menu.addAction(act_read)

        # 2026-09-09 v1.7.2:用户元数据菜单 — 单条才允许
        menu.addSeparator()
        act_fav = QAction(view.tr("★ 收藏 / 取消收藏"), menu)
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

        act_tags = QAction(view.tr("🏷 设置标签…"), menu)
        act_tags.setEnabled(count == 1)
        if count == 1 and sel:
            cid, mid = sel[0][0], sel[0][1]
            act_tags.triggered.connect(lambda: self.tags_requested.emit(cid, mid))
        menu.addAction(act_tags)

        act_notes = QAction(view.tr("📝 设置备注…"), menu)
        act_notes.setEnabled(count == 1)
        if count == 1 and sel:
            cid, mid = sel[0][0], sel[0][1]
            act_notes.triggered.connect(lambda: self.notes_requested.emit(cid, mid))
        menu.addAction(act_notes)

        menu.exec_(global_pos)
