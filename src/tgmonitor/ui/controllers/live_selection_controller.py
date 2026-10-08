"""LIVE 多选 + 详情入口 controller — `_on_live_*` 14 handler + `_run_live_*` 7 helper。

2026-10-04 plan 阶段 3.3:从 `MainWindow` 抽出的最大 controller(14 handler +
7 helper + 1 dialog helper + selection 共享 state)。模板样板延续自
SearchController(plain class + `MainWindowCtx`,无 MainWindow 引用)。

设计要点:
- `_last_live_selection` 由 controller 持有(原 MainWindow 上)— toolbar 6
  个按钮按需读;selectionModel 已 clear 时不依赖 `live_view` 当前状态
- 翻译走 `QCoreApplication.translate("MainWindow", src)` 沿用 zh_CN.ts / en_US.ts
- `parent=self`(MainWindow)显式参数化:`QMessageBox.warning(self.main_window, ...)`
  与 `ChannelPickerDialog.pick_channel(self.main_window, channels)` 等需要
  Qt parent widget 的对话框走 `ctx.main_window` 拿

信号契约(全部连到 controller 公开方法,不存于 `_wire_signals` 集中表):
- `live_view.selection_count_changed` → `on_selection_count_changed`
- `live_view.selection_messages_changed` → `on_selection_messages_changed`
- `live_view.export_requested` / `delete_requested` / `mark_read_requested` /
  `favorite_requested` / `tags_requested` / `notes_requested` → 6 个右键入口
- `selection_toolbar.{export,delete,mark_read,forward,pin,react}_clicked` →
  `on_X_selected()` 6 个 toolbar 入口(读 `_last_live_selection`)
- `message_detail.export_current_requested` / `delete_current_requested` /
  `copy_text_requested` → 3 个详情顶部按钮入口
"""

# mypy: disable-error-code="attr-defined"
# PySide6 6.6+.pyi 漏 QMessageBox.Yes/No 等 class-level const — 同 main_window.py
# 顶层 disable-error-code 注释,统一关掉 attr-defined。

from __future__ import annotations

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.ui.controllers._base import MainWindowCtx


def _normalize_selection_items(items: list | None) -> list[tuple[int, int]]:
    """过滤 None / 长度不足 / 非数字项,返 `(cid, mid)` 列表。

    `items` 来源两种:
    - `MessageView.selection_messages_changed` 信号:`list[tuple[cid, mid]]`
    - 兼容性容错(未来 media manager 风格复用):`list[(cid, mid, idx)]`

    2026-09-08 v1.7.0:原在 main_window.py module level;2026-10-04 plan
    阶段 3.3 搬到 controller(只有 live_selection 用)。
    """
    if not items:
        return []
    out: list[tuple[int, int]] = []
    for it in items:
        if isinstance(it, tuple) and len(it) >= 2:
            out.append((int(it[0]), int(it[1])))
    return out


class LiveSelectionController:
    """LIVE 多选 + 详情入口 + 批量动作编排。

    共享 state:`_last_live_selection`(toolbar 按钮按下时缓存最近一次 selection,
    不依赖 `live_view.selectionModel` 当前状态)。
    """

    def __init__(self, ctx: MainWindowCtx) -> None:
        self._ctx = ctx
        # 2026-09-08 v1.7.0:LIVE 多选 toolbar 缓存最近一次 selection —
        # toolbar 按钮按下时不必重新问 live_view(selection 可能已被 toolbar
        # clear 触发清空 — 但 toolbar.clear 是后置动作,handler 触发顺序:按钮
        # click → handler → clear,所以读到的是上一帧选择)。
        self._last_live_selection: list[tuple[int, int]] = []

    # ============================================================
    # Selection state 维护(由 live_view signal 驱动)
    # ============================================================

    def on_selection_count_changed(self, count: int) -> None:
        """LIVE 多选数量变化 → toolbar 显示 / 隐藏。

        count=0 → 隐藏(selectionCleared 也会走这里);count>0 → 显示并
        触发 `on_selection_messages_changed` 缓存最后选择(供 toolbar 按钮
        后续 lambda 读取 — toolbar 按钮按下时 selectionModel 可能已被
        clear 触发清空)。
        """
        self._ctx.selection_toolbar.setVisible(count > 0)

    def on_selection_messages_changed(self, items: list) -> None:
        """缓存最近一次完整 selection + 同步 toolbar 计数 label。

        items 是 list[tuple[cid, mid]] 或 list[(cid, mid, idx)] — 我们只关心 (cid, mid)
        """
        # items 是 list[tuple[cid, mid]] 或 list[(cid, mid, idx)] — 我们只关心 (cid, mid)
        normalized = _normalize_selection_items(items)
        self._last_live_selection = normalized
        # 同步更新 toolbar 计数 label
        self._ctx.selection_toolbar.set_count(len(normalized))

    # ============================================================
    # 右键菜单(LIVE 多选)入口 — 走 `on_live_X(items)`
    # ============================================================

    def on_live_export(self, items: list) -> None:
        """右键菜单触发 → 弹 ExportDialog(selected_messages=items)。

        items 是 list[(cid, mid)],与 Media Manager 「导出 CSV」流程分开,
        走 ExportDialog(让用户选 JSON/HTML/ZIP + 输出路径)。
        """
        normalized = _normalize_selection_items(items)
        if not normalized:
            return
        # 复用现有 ExportDialog,在外部再注入 selected_messages
        # (ExportDialog 当前只支持 channel_ids / single_message_id,本 PR
        # 扩展它接受 selected_messages — 详见 export_dialog.py 改动)。
        from tgmonitor.ui.widgets.export_dialog import ExportDialog  # 局部 import 避开循环

        dlg = ExportDialog(
            self._ctx.app,
            channel_ids=[],
            selected_messages=normalized,
        )
        if not dlg.exec():
            return
        self._run_live_export(dlg.request())

    def on_live_delete(self, items: list) -> None:
        """右键菜单触发 → 二次确认 → 批量删(LIVE 软删 + media cleanup)。

        走 `AppService.delete_messages_batch`(返成功数);不调 TDLib
        `deleteMessages`(live 删除本地副本,TG 端不动 — 与 Clear Channel
        行为对齐,v1.7.0 不引入服务端删除)。
        """
        normalized = _normalize_selection_items(items)
        if not normalized:
            return
        from PySide6.QtWidgets import QMessageBox

        mw = self._ctx.main_window
        n = len(normalized)
        ans = QMessageBox.warning(
            mw,
            self._tr("删除确认"),
            # 2026-09-08 v1.7.0:%d 占位 — `self.tr()` 返回翻译后再 `% n` 插值。
            # 不能用 f-string(lupdate 抽的是模板,翻译后 `{n}` 不再被替换)。
            self._tr("确定删除选中的 %d 条消息?\n删除后无法撤销。") % n,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        self._run_live_delete(normalized)
        # 删完顺手 clear selection(避免用户在空 row 上右键菜单)
        self._ctx.live_view.clear_selection()

    def on_live_mark_read(self, items: list) -> None:
        """右键菜单触发 → 批量标已读(走 TDLib viewMessages)。

        走 `AppService.mark_messages_read`(返成功条数);paused 时返 0
        + log warning,不抛 UI 错误。
        """
        normalized = _normalize_selection_items(items)
        if not normalized:
            return
        self._run_live_mark_read(normalized)
        # 标已读不改本地视图,不清 selection(用户可能想接着操作)

    def on_live_favorite(self, channel_id: int, telegram_msg_id: int, value: bool) -> None:
        """右键菜单 ★ 触发 → 调 AppService.set_favorite。

        `value` 由右键菜单根据当前 is_favorite 翻转传进来(选中=False → 传 True)。
        """
        from tgmonitor.ui.async_bridge import run_coro

        run_coro(
            self._ctx.loop,
            self._ctx.app.set_favorite(channel_id, telegram_msg_id, value),
            error_label="live_favorite",
        )

    def on_live_tags(self, channel_id: int, telegram_msg_id: int) -> None:
        """右键菜单 🏷 触发 → 弹 tag 输入 → 调 AppService.set_tags。

        逗号 / 空格分隔多个 tag;空白过滤。
        """
        from PySide6.QtWidgets import QInputDialog

        from tgmonitor.ui.async_bridge import run_coro

        mw = self._ctx.main_window
        text, ok = QInputDialog.getText(
            mw,
            self._tr("设置标签"),
            self._tr("输入标签(逗号或空格分隔,如 tech, news):"),
        )
        if not ok:
            return
        tags = [t.strip() for t in text.replace(",", " ").split() if t.strip()]
        run_coro(
            self._ctx.loop,
            self._ctx.app.set_tags(channel_id, telegram_msg_id, tags),
            error_label="live_tags",
        )

    def on_live_notes(self, channel_id: int, telegram_msg_id: int) -> None:
        """右键菜单 📝 触发 → 多行输入框 → 调 AppService.set_notes。"""
        # 用 QInputDialog.getMultiLineText 替代单行输入 — 备注通常多行。
        from PySide6.QtWidgets import QInputDialog

        from tgmonitor.ui.async_bridge import run_coro

        mw = self._ctx.main_window
        text, ok = QInputDialog.getMultiLineText(
            mw,
            self._tr("设置备注"),
            self._tr("为本条消息添加备注(支持多行):"),
        )
        if not ok:
            return
        run_coro(
            self._ctx.loop,
            self._ctx.app.set_notes(channel_id, telegram_msg_id, text),
            error_label="live_notes",
        )

    # ============================================================
    # Toolbar 入口(读 `_last_live_selection`,无参版本)
    # ============================================================

    def on_live_export_selected(self) -> None:
        """toolbar 导出按钮 — 用缓存的 `_last_live_selection`。"""
        self.on_live_export(self._last_live_selection)

    def on_live_delete_selected(self) -> None:
        """toolbar 删除按钮 — 用缓存的 `_last_live_selection`。"""
        self.on_live_delete(self._last_live_selection)

    def on_live_mark_read_selected(self) -> None:
        """toolbar 标已读按钮 — 用缓存的 `_last_live_selection`。"""
        self.on_live_mark_read(self._last_live_selection)

    def on_live_forward_selected(self) -> None:
        """2026-09-09 v1.7.2:toolbar 转发按钮。"""
        self._on_live_forward(self._last_live_selection)

    def on_live_pin_selected(self) -> None:
        """2026-09-09 v1.7.2:toolbar 钉选按钮。"""
        self._on_live_pin(self._last_live_selection)

    def on_live_react_selected(self) -> None:
        """2026-09-09 v1.7.2:toolbar emoji 回应按钮。"""
        self._on_live_react(self._last_live_selection)

    # ============================================================
    # 详情顶部 3 按钮入口(单条操作)
    # ============================================================

    def on_live_export_current(self, channel_id: int, telegram_msg_id: int) -> None:
        """详情顶部「导出」→ 单条消息导出(走 ExportDialog)。"""
        from tgmonitor.ui.widgets.export_dialog import ExportDialog

        dlg = ExportDialog(
            self._ctx.app,
            channel_ids=[channel_id],
            single_message_id=telegram_msg_id,
        )
        if not dlg.exec():
            return
        self._run_live_export(dlg.request())

    def on_live_delete_current(self, channel_id: int, telegram_msg_id: int) -> None:
        """详情顶部「删除」→ 二次确认 → 单条删 + 关详情面板。"""
        from PySide6.QtWidgets import QMessageBox

        mw = self._ctx.main_window
        ans = QMessageBox.warning(
            mw,
            self._tr("删除确认"),
            self._tr("确定删除这条消息?\n删除后无法撤销。"),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        self._run_live_delete([(channel_id, telegram_msg_id)])
        # 关详情 — 当前消息已删,留着也没意义
        self._ctx.message_detail.show_message(None)

    def on_live_copy_text(self, text: str) -> None:
        """详情顶部「复制文本」→ 仅做 toast 提示(实际 copy 在 MessageDetail
        handler 里走 QGuiApplication.clipboard().setText)。

        上层这里只 log + status bar 显示「已复制」;未来可接 toast 系统。
        """
        self._ctx.status_bar.show_message(self._tr("已复制"), 2000)

    # ============================================================
    # 内部:_on_live_X(items)— toolbar 用(走 `_last_live_selection`)
    # 与 `_run_live_*` 编排层
    # ============================================================

    def _on_live_forward(self, items: list) -> None:
        """2026-09-09 v1.7.2:批量转发入口 — 弹目标频道选择 dialog,接收 ID 后转发。

        2026-09-11 v1.7.4:升级为 ChannelPickerDialog(列表 + 搜索 + 双击),取代
        QInputDialog.getInt 输入整数 chat_id(易输错,无候选)。
        """
        normalized = _normalize_selection_items(items)
        if not normalized:
            return
        from tgmonitor.ui.async_bridge import run_coro

        async def _go() -> list[ChannelDTO]:
            return await self._ctx.app.list_joined_channels()

        def _on_channels(channels: list[ChannelDTO]) -> None:
            # 局部 import — channel picker 是次常用路径,避免启动开销。
            from tgmonitor.ui.widgets.channel_picker_dialog import ChannelPickerDialog

            mw = self._ctx.main_window
            to_cid = ChannelPickerDialog.pick_channel(mw, channels)
            if to_cid is None:
                return
            self._run_live_forward(normalized, to_cid)
            self._ctx.live_view.clear_selection()

        run_coro(self._ctx.loop, _go(), on_success=_on_channels, error_label="live_forward_pick")

    def _on_live_pin(self, items: list) -> None:
        """2026-09-09 v1.7.2:批量钉选入口。

        2026-09-13 v1.7.5 PR #3 (P0-C):二次确认 — 钉选是「写服务端 +
        不能 quick undo」的操作,与批量删除对齐。
        """
        normalized = _normalize_selection_items(items)
        if not normalized:
            return
        from PySide6.QtWidgets import QMessageBox

        mw = self._ctx.main_window
        n = len(normalized)
        ans = QMessageBox.warning(
            mw,
            self._tr("钉选确认"),
            self._tr("确定钉选选中的 %d 条消息?\n操作不可撤销。") % n,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        self._run_live_pin(normalized)
        self._ctx.live_view.clear_selection()

    def _on_live_react(self, items: list) -> None:
        """2026-09-09 v1.7.2:批量 emoji 回应入口 — 弹 QInputDialog 输入 emoji 字符。

        2026-09-11 v1.7.4:升级为 EmojiPickerDialog(60+ grid + is_big + 手输兜底),
        取代 QInputDialog.getText。
        2026-09-13 v1.7.5 PR #3 (P0-C):emoji 选完后弹二次确认 — 批量打
        emoji 是「写服务端」操作,不可撤销,与 pin / delete 对齐。
        """
        normalized = _normalize_selection_items(items)
        if not normalized:
            return
        # 局部 import — emoji picker 是次常用路径,避免 main_window 启动开销。
        from PySide6.QtWidgets import QMessageBox

        from tgmonitor.ui.widgets.emoji_picker_dialog import EmojiPickerDialog

        mw = self._ctx.main_window
        result = EmojiPickerDialog.get_emoji(mw)
        if result is None:
            return
        emoji, is_big = result
        n = len(normalized)
        ans = QMessageBox.warning(
            mw,
            self._tr("回应确认"),
            self._tr("确定对选中的 %d 条消息打 %s 反应?\n操作不可撤销。") % (n, emoji),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        self._run_live_react(normalized, emoji, is_big=is_big)
        self._ctx.live_view.clear_selection()

    def _run_live_export(self, req) -> None:
        """透传 ExportDialog 拼好的 ExportRequest → 走 ExportProgressDialog。

        req 来自 `ExportDialog.request()`,字段(selected_messages /
        single_message_id / channel_ids / format / ...)已组装好。复用现有
        `_on_export_done` / `_on_export_done_with_error` 反馈路径。
        """

        # 2026-09-08 v1.7.0:`app.export` 是 AsyncIterator,不能直接传 run_coro
        # (需 Coroutine)。包成 fire-and-forget 协程消耗迭代器,跟 VM.start_export
        # 一致。
        from tgmonitor.ui.async_bridge import run_coro

        async def _go() -> None:
            async for _ in self._ctx.app.export(req):
                pass

        run_coro(self._ctx.loop, _go(), error_label="live_export")

    def _run_live_delete(self, items: list[tuple[int, int]]) -> None:
        """批量删 — `AppService.delete_messages_batch` 返成功数。

        通过 VM.publish_threadsafe 走 async loop,UI 不阻塞。
        2026-09-09 v1.7.2:加 BatchProgressDialog 显示进度。
        """
        if not items:
            return
        from tgmonitor.ui.async_bridge import run_coro

        self._show_batch_progress_dialog("delete")
        run_coro(
            self._ctx.loop,
            self._ctx.app.delete_messages_batch(items),
            error_label="live_delete",
        )

    def _run_live_mark_read(self, items: list[tuple[int, int]]) -> None:
        """批量标已读 — `AppService.mark_messages_read` 返成功条数。

        paused 时返 0 + log warning,不抛 UI 错误。
        """
        if not items:
            return
        from tgmonitor.ui.async_bridge import run_coro

        run_coro(
            self._ctx.loop,
            self._ctx.app.mark_messages_read(items),
            error_label="live_mark_read",
        )

    def _run_live_forward(self, items: list[tuple[int, int]], to_chat_id: int) -> None:
        """2026-09-09 v1.7.2:批量转发 — `AppService.forward_messages`。
        配合 `_show_batch_progress_dialog` 在 LIVE 选多条 → 转发到… →
        弹目标频道选择 → 触发本方法。
        """
        if not items:
            return
        from tgmonitor.ui.async_bridge import run_coro

        self._show_batch_progress_dialog("forward")
        run_coro(
            self._ctx.loop,
            self._ctx.app.forward_messages(items, to_chat_id),
            error_label="live_forward",
        )

    def _run_live_pin(self, items: list[tuple[int, int]]) -> None:
        """2026-09-09 v1.7.2:批量钉选。"""
        if not items:
            return
        from tgmonitor.ui.async_bridge import run_coro

        self._show_batch_progress_dialog("pin")
        run_coro(self._ctx.loop, self._ctx.app.pin_messages(items), error_label="live_pin")

    def _run_live_react(
        self, items: list[tuple[int, int]], emoji: str, *, is_big: bool = False
    ) -> None:
        """2026-09-09 v1.7.2:批量 emoji 回应。

        2026-09-10 v1.7.3:`extra=emoji` 传给 BatchProgressDialog,标题显示
        「批量回应 😀 中…」(unreact 类似)。
        2026-09-11 v1.7.4:`is_big` 透传到 AppService.add_reaction — 由
        EmojiPickerDialog 的「大表情」checkbox 决定。
        """
        if not items or not emoji:
            return
        from tgmonitor.ui.async_bridge import run_coro

        self._show_batch_progress_dialog("react", extra=emoji)
        run_coro(
            self._ctx.loop,
            self._ctx.app.add_reaction(items, emoji, is_big=is_big),
            error_label="live_react",
        )

    def _show_batch_progress_dialog(self, op: str, extra: str = "") -> None:
        """2026-09-09 v1.7.2:构造 + show 非模态 BatchProgressDialog,
        VM signal 驱动进度。`op` 用于标题(`forward` / `pin` / `react` 等)。

        无返回 — 调用方 fire-and-forget,VM `batch_done` signal 触发
        `dialog.accept()` 收尾。

        2026-09-10 v1.7.3:`extra` 在 react/unreact 时传入 emoji,标题插入;
        其他 op 忽略。
        """
        from tgmonitor.ui.widgets.batch_progress_dialog import BatchProgressDialog

        dlg = BatchProgressDialog(self._ctx.vm, op=op, extra=extra, parent=self._ctx.main_window)
        dlg.show()

    def _tr(self, src: str) -> str:
        """翻译入口 — 保留 `MainWindow` context,沿用现有 zh_CN.ts / en_US.ts 翻译。

        原 MainWindow 自有 `self.tr(src)`,context = `MainWindow`;controller
        没 QWidget 引用,走 `QCoreApplication.translate` 全局入口。
        """
        from PySide6.QtCore import QCoreApplication

        return QCoreApplication.translate("MainWindow", src)
