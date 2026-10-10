# mypy: disable-error-code=attr-defined
"""窗口生命周期 + 全局快捷键行为 controller。

2026-10-10 v1.12.x 从 `MainWindow` 抽提。F 类 110 LOC 集中
window/quit/escape/copy/focus/navigation 6 个动作:
- `show_and_raise`:unminimize + 顶置 + 焦点(File / tray「显示主窗口」共用)
- `quit_app`:标 `_truly_quit=True` + `qt_app.quit()`(走 aboutToQuit
  → `_shutdown_then_quit` 同步等完成)
- `switch_tab`:切 nav(`_on_nav_changed` 自动 emit → stack 跟随)
- `on_nav_changed`:切到 MEDIA 页(3)触发一次列表刷新
- `focus_search`:聚焦到搜索框 + 切到 LIVE 页 + 全选
- `on_global_escape`:Esc 全局快捷键(搜索框 focus → 清空 + 失焦;
  其它页 → 回 LIVE)
- `copy_current_message_text`:Ctrl+C 复制当前 LIVE 选中消息 text

设计:plain class + `MainWindowCtx`。与 `ShortcutController` 不同,本
controller **不通过 thin delegate 保留方法名** — 7 个 method 全是
MainWindow 公开的 slot(signal 触发),Signal wire 全在 MainWindow
里直接连到 `self._lifecycle_ctrl.show_and_raise()` 等。`QuitRequested`
signal 仍走 `tray_controller._on_vm_quit_requested` 路径(那个有
tray 通知交互),不走本 controller。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QApplication

if TYPE_CHECKING:
    from tgmonitor.ui.controllers._base import MainWindowCtx

log = logging.getLogger(__name__)


class LifecycleController:
    """窗口生命周期 + 全局快捷键行为(quit/show/escape/copy/focus/nav)。"""

    def __init__(self, ctx: MainWindowCtx) -> None:
        self._ctx = ctx

    # ---- 窗口 show / quit ----

    def show_and_raise(self) -> None:
        """File / tray「显示主窗口」共用 — unminimize + 顶置 + 焦点。"""
        mw = self._ctx.main_window
        mw.showNormal()
        mw.raise_()
        mw.activateWindow()

    def quit_app(self) -> None:
        """File→Quit / Ctrl+Q / tray「退出」共用 — `qt_app.quit()` 触发
        aboutToQuit → `app.py:_shutdown_then_quit` 同步等 `_shutdown_async`
        完成后真 quit。

        2026-09-22 v1.8.x:之前路径是 `_truly_quit=True; qt_app.quit()`,
        Qt 注释误以为「qt_app.quit() → 各 QWindow 关 → closeEvent 触发」,
        但实测 `qt_app.quit()` **不会**自动 close 窗口,只退出事件循环 —
        closeEvent 不会因此触发,`_shutdown_then_quit`(aboutToQuit handler)
        调度 future,qasync 不保证 pump,future 可能直接被 loop close cancel。

        修法:`_shutdown_then_quit` 改为嵌套 subloop 同步等(与 closeEvent 同
        模式),所有 quit 路径(File→Quit / Ctrl+Q / tray「退出」/ SIGINT /
        SIGTERM / macOS dock Cmd+Q)都收敛到 aboutToQuit → 真同步跑完
        shutdown。`setQuitOnLastWindowClosed(False)` 让 `_truly_quit=False`
        的关窗走 hide(不退出),`_truly_quit=True` 走真退出。
        """
        mw = self._ctx.main_window
        mw._truly_quit = True
        qt_app = QApplication.instance()
        assert qt_app is not None, "QApplication gone before quit"
        qt_app.quit()  # → aboutToQuit → _shutdown_then_quit 同步等完成 → loop close

    # ---- 导航 ----

    def switch_tab(self, idx: int) -> None:
        self._ctx.nav.set_current(idx)
        # nav.set_current 已经 emit current_changed,stack 会自动跟

    def on_nav_changed(self, idx: int) -> None:
        """nav tab 切换 — 切到 MEDIA 页(3)触发一次列表刷新。

        其他页忽略。VM 异步拉数据,第一次可能因 known_channels 还没就绪而
        短暂为空,后续 channels_changed → _refresh_state 也会再刷一次。
        """
        if idx == 3:
            # 走 `media_refresh_debounce` timer → `MediaActionsController.
            # flush_media_refresh_pending` → `vm.refresh_media_list`(节流
            # 避免快速切 tab 时反复拉)。
            #
            # 2026-10-10 修:之前 main_window._on_media_refresh() 是 stale
            # call — 该方法在 plan-stage3.3 抽 MediaActionsController 时已删,
            # 本 controller 改成 start debounce timer,既走通「切到 MEDIA →
            # 拉一次」的语义,又保留节流行为。
            self._ctx.media_refresh_debounce.start()

    def focus_search(self) -> None:
        """聚焦到搜索框 + 自动切到 LIVE 页(搜索只在消息视图里有意义)"""
        self.switch_tab(0)
        self._ctx.header.search_bar.edit.setFocus()
        self._ctx.header.search_bar.edit.selectAll()

    # ---- 全局 Esc ----

    def on_global_escape(self) -> None:
        """2026-08-30 v1.5.0 PR #A5:Esc 全局快捷键。

        优先级:
        1. 搜索框有 focus → 清空搜索 + 失焦(回 LIVE)
        2. 设置页 / 子 dialog 有 focus → 退到默认(LIVE)
        3. 否则 no-op(避免误清 LIVE 选中)

        2026-09-02 v1.5.2 PR #B5:搜索框 clear 同时清 date panel + 折叠按钮 +
        stop debounce timer(避免 stale fetch 跑到空 query 上)。
        """
        sb = self._ctx.header.search_bar.edit
        if sb.hasFocus():
            # 2026-09-02 PR #B5:stop timer 防 stale search 在 clear 后继续
            # 触发(用户在 300ms 内按 Esc 但 search 已 emit 过);直接 clear()
            # 会走 SearchBar._on_text_changed → 启新 timer,这里提前 stop。
            self._ctx.search_debounce.stop()
            self._ctx.header.search_bar.clear()  # SearchBar.clear() 重置 text + dates + adv
            sb.clearFocus()
            # 主动 emit `[]` → 清空视图(后续 live 消息自然 append 重建 LIVE 流)
            self._ctx.live_view.set_messages([])
            return
        # 若当前不在 LIVE,回 LIVE(简化 — 用户感知的「取消」)
        if self._ctx.stack.currentIndex() != 0:
            self.switch_tab(0)

    # ---- Ctrl+C 复制 ----

    def copy_current_message_text(self) -> None:
        """2026-08-30 v1.5.0 PR #A5:Ctrl+C 复制当前 LIVE 选中消息 text。

        2026-09-02 v1.5.3 PR #D1:MessageView 由 `QListWidget` 升级为 `QListView`
        + `MessageListModel`,`currentItem().data(Qt.UserRole)` 协议废除,
        改用 `live_view.current_message()` helper(MessageView 新增 method)
        走 `model.data(currentIndex, DtoRole)` 拿 MessageDTO。
        """
        if self._ctx.stack.currentIndex() != 0:
            return  # 只在 LIVE 页生效
        msg = self._ctx.live_view.current_message()
        if msg is None:
            return
        text = getattr(msg, "text", None) or ""
        if not text:
            self._ctx.status_bar.show_message(self._ctx.main_window.tr("当前消息无文本"), 1500)
            return
        QApplication.clipboard().setText(text)
        self._ctx.status_bar.show_message(
            self._ctx.main_window.tr("已复制 {n} 字").format(n=len(text)), 1500
        )
