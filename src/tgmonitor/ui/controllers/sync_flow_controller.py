# mypy: disable-error-code=attr-defined
"""全量同步流程 controller — `_on_sync_requested` + 3 helper。

2026-10-09 v1.12.x 从 `MainWindow` 抽提。E 类 79 LOC 集中
`_on_sync_requested` orchestrator + `_build_sync_titles` +
`_show_sync_options_dialog` + `_wire_sync_progress_signals`。

设计:plain class + `MainWindowCtx`。`SyncController.on_sync_all_channels`
通过 `mw._on_sync_requested(ids)` 调用 `MainWindow` 留的 thin
delegate,后者转给本 controller 的 `on_sync_requested(ids)`。
"""

from __future__ import annotations

from tgmonitor.core.dto import SyncOptions
from tgmonitor.ui.async_bridge import run_coro
from tgmonitor.ui.controllers._base import MainWindowCtx
from tgmonitor.ui.widgets.sync_dialog import SyncOptionsDialog, SyncProgressDialog


class SyncFlowController:
    """全量同步请求编排器 — 弹 options dialog + 启动 progress + 跑 coroutine。"""

    def __init__(self, ctx: MainWindowCtx) -> None:
        self._ctx = ctx

    def on_sync_requested(self, channel_ids: list[int]) -> None:
        """同步请求入口 — 编排 4 个 helper 把 38 行展开成 10 行 orchestrator。

        流程:
          1. titles dict 从 VM known_channels 拉(channel id → title)
          2. show options dialog → user 取消或 settings 不合规就 None
          3. open progress dialog + connect VM signals to it
          4. run_coro 后台 fire 同步 — 协程 finally 里清理 signal connections
        """
        titles = self._build_sync_titles(channel_ids)
        options = self._show_sync_options_dialog(channel_ids, titles)
        if options is None:
            return

        progress_dlg = SyncProgressDialog(
            titles,
            cancel_cb=self._ctx.app.channel_sync.cancel,
            parent=self._ctx.main_window,
        )
        self._wire_sync_progress_signals(progress_dlg)

        async def _go() -> None:
            try:
                await self._ctx.app.sync_channels(channel_ids, options)
            finally:
                # progress dialog 关闭后 disconnect VM signals — 防 dangling slot
                # Qt C++ 侧 reference 累积。dialog 已 GC / signal 已断 → swallow
                # RuntimeError / TypeError(Qt 已 disconnect 后再调 disconnect 抛)
                try:
                    self._ctx.vm.sync_progress.disconnect(progress_dlg.on_progress)
                    self._ctx.vm.sync_done.disconnect(progress_dlg.on_done)
                except (RuntimeError, TypeError):
                    pass

        run_coro(self._ctx.loop, _go(), error_label="sync_with_progress")
        progress_dlg.exec()

    def _build_sync_titles(self, channel_ids: list[int]) -> dict[int, str]:
        """`channel_id → title` 给 SyncOptionsDialog / SyncProgressDialog 显示用。

        VM 里有 DTO 用 `.title`,没找到(VMe 还没 bootstrap)回退 `#<id>`。
        """
        titles: dict[int, str] = {}
        for cid in channel_ids:
            ch = self._ctx.vm.known_channels.get(cid)
            titles[cid] = ch.title if ch else f"#{cid}"
        return titles

    def _show_sync_options_dialog(
        self,
        channel_ids: list[int],
        titles: dict[int, str],
    ) -> SyncOptions | None:
        """弹 SyncOptionsDialog + 读取 user 选择的选项。

        返回 None:用户取消 OR 校验失败(让 options() 返 None)— 调用方不继续。
        """
        defaults = SyncOptions(
            chat_delay_ms=self._ctx.app.settings.sync_chat_delay_ms,
            page_delay_ms=self._ctx.app.settings.sync_page_delay_ms,
            resume_from_saved=self._ctx.app.settings.sync_resume_from_saved,
        )
        dlg = SyncOptionsDialog(channel_ids, titles, defaults, self._ctx.main_window)
        if not dlg.exec():
            return None
        return dlg.options()

    def _wire_sync_progress_signals(self, dialog: SyncProgressDialog) -> None:
        """把 VM 的 sync_progress / sync_done Signal 连到 progress dialog 的 slot。

        解绑在 `on_sync_requested._go()` 的 `finally` 里做(避免 dialog 已
        destroy 时 disconnect 抛 RuntimeError 的处理)。
        """
        self._ctx.vm.sync_progress.connect(dialog.on_progress)
        self._ctx.vm.sync_done.connect(dialog.on_done)
