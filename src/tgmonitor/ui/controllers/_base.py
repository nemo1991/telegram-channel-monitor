# mypy: disable-error-code=attr-defined
"""Controller 共享 base — `MainWindowCtx` dataclass。

2026-10-01 v1.12.x:`MainWindow` 拆 6 个低风险 per-domain controller
(theme / tray / header_action / settings_reaction / export / sync),
复杂 4 个 (live_selection / media_actions / search / message_stream) 留
`main_window.py`,跟随后续 PR。

2026-10-04 plan 阶段 3.3:4 复杂 controller 落点。
2026-10-04 plan 阶段 3.4:加 `window_title_setter` / `quit_app` 反向调用
callback — 替代 `tray_controller` 直接调 `main_window.setWindowTitle` /
`main_window._quit_app`,MainWindow 注入 lambda,controller 不持有反向引用。

为什么 dataclass + shared reference,不是 enum-style config:
- controller 需要 widget refs (live_view / status_bar / header / nav / ...)
- 还要共享 mutable 状态(`_last_live_selection` / `_shutdown_cb` /
  `_truly_quit` / `_tray_first_close_hint_shown`)
- dataclass 字段按需暴露,MainWindow `__init__` 末尾一次性灌入;
  后续 controller 引用不到 ctx 里的字段时是「漏暴露」,加字段即可

设计:plain class 不是 QObject。controller 不持有 Qt 主线程对象
(只是引用 widget),不参与 Qt 元对象系统。signal/slot 仍走
`MainWindow._on_X → self._X_ctrl.method(*a, **kw)` thin delegate。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow, QStackedWidget

from tgmonitor.core.app_service import AppService
from tgmonitor.core.monitor.service import MonitorService

# 2026-10-15 plan-batch7-C-α:LiveActionsController 在 ui/controllers/ 目录下,
# 导入放最末(避免与 message_view 反向 import 链产生循环 — 实际上没有,
# 但置末以示「Step C 新增」)
from tgmonitor.ui.controllers.live_actions_controller import LiveActionsController
from tgmonitor.ui.nav_bar import VerticalNavBar
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel
from tgmonitor.ui.widgets.channel_widget import ChannelWidget
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget
from tgmonitor.ui.widgets.header_bar import HeaderBar
from tgmonitor.ui.widgets.media_manager_widget import MediaManagerWidget
from tgmonitor.ui.widgets.message_detail import MessageDetail
from tgmonitor.ui.widgets.message_view import MessageView
from tgmonitor.ui.widgets.selection_toolbar import SelectionToolbar
from tgmonitor.ui.widgets.status_bar import StatusBar
from tgmonitor.ui.widgets.tray_icon import TrayIcon


@dataclass
class MainWindowCtx:
    """MainWindow 拆 controller 时共享的字段。

    设计:
    - 字段按 controller 需要按需暴露;不强制一次性填齐
    - plain class(controller)接受 ctx,只读字段,避免反向修改污染 state
    - MainWindow 仍持所有真实状态,controller 不重复持有
    - `main_window` 字段持 MainWindow QWidget 引用 — controller 需要
      作 `parent=QWidget` 给 dialog(LoginDialog / BatchProgressDialog /
      QMessageBox 等)时通过此字段访问;反向"调出去"动作(改窗口标题
      / 触发退出)走 `window_title_setter` / `quit_app` callback,不直接
      持 `main_window` 反向方法调用。
    """

    app: AppService
    monitor: MonitorService
    loop: asyncio.AbstractEventLoop
    vm: MonitorViewModel
    env_path: Path

    # MainWindow QWidget 引用(controller 作 dialog parent 时用)
    main_window: QMainWindow

    # 反向调用 callback(2026-10-04 plan 阶段 3.4)— MainWindow 注入 lambda,
    # controller 调出去改 window 状态但不需要碰 QWidget 引用本身。
    # - `window_title_setter(title)` 等价 `main_window.setWindowTitle(title)`
    # - `quit_app()` 等价 `main_window._quit_app()`(走 `qt_app.quit()`)
    window_title_setter: Callable[[str], None]
    quit_app: Callable[[], None]

    # Widget refs(按需)
    live_view: MessageView
    # 2026-10-15 plan-batch7-C-α:LiveActionsController(QObject)— 8 Signal
    # (selection_count_changed / selection_messages_changed / export_requested
    # / delete_requested / mark_read_requested / favorite_requested /
    # tags_requested / notes_requested)+ 右键菜单 + 选区广播
    # 从 MessageView 抽到独立 controller,公开 API 锁由 controller 持有。
    live_actions: LiveActionsController
    message_detail: MessageDetail
    media_manager: MediaManagerWidget
    selection_toolbar: SelectionToolbar
    dashboard: DashboardWidget
    channel_panel: ChannelWidget
    header: HeaderBar
    status_bar: StatusBar
    nav: VerticalNavBar
    stack: QStackedWidget
    tray: TrayIcon | None

    # QTimer(shared)
    search_debounce: QTimer
    media_refresh_debounce: QTimer
