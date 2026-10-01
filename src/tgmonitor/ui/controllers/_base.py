# mypy: disable-error-code=attr-defined
"""Controller 共享 base — `MainWindowCtx` dataclass。

2026-10-01 v1.12.x:`MainWindow` 拆 6 个低风险 per-domain controller
(theme / tray / header_action / settings_reaction / export / sync),
复杂 4 个 (live_selection / media_actions / search / message_stream) 留
`main_window.py`,跟随后续 PR。

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

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow, QStackedWidget

from tgmonitor.core.app_service import AppService
from tgmonitor.core.monitor.service import MonitorService
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
    - `main_window` 字段持有 MainWindow QWidget 引用 — controller 需要
      调 `setWindowTitle` / `close` / `show` 等 QWidget 方法时通过此
      字段访问(避免 controller 重复持有 widget state)
    """

    app: AppService
    monitor: MonitorService
    loop: asyncio.AbstractEventLoop
    vm: MonitorViewModel
    env_path: Path

    # MainWindow QWidget 引用(controller 需 setWindowTitle / close 时用)
    main_window: QMainWindow

    # Widget refs(按需)
    live_view: MessageView
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
