# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""SystemMixin — 状态栏 + 系统事件 handler 集合 — 2026-10-19 plan-batch9-γ。

`MonitorViewModel` 拆 5 mixin 第三步(2026-10-19
plan-batch9-γ):system 域 = 7 handler + 4 status bar signal +
1 method + `_message_count` 状态字段。

- handler(7):`_on_login_state` / `_on_conn_state` /
  `_on_quit_requested`(含 pause 调 `app.pause_monitor` /
  `resume_monitor`)/ `_on_error` / `_on_settings_changed`(emit
  `settings_changed` + `backend_label_changed`)/ `_on_sync_progress` /
  `_on_sync_done`
- status bar signal(4):`selected_channel_changed` /
  `backend_label_changed` / `last_message_received` / `stats_changed`
  — 2026-10-01 v1.11.x 状态栏组件化,4 个 payload 透传
- method:`set_selected_channel`(emit `selected_channel_changed` 给
  `_SelectedChannelLabel`)
- 状态字段:`_message_count: int = 0`(由 facade `__init__` 显式设;
  跨 mixin 累加,本 mixin 不写 `__init__`)

跨 mixin access:`_on_login_state` 不跨;`_on_quit_requested` 调
`self.app.is_paused / pause_monitor / resume_monitor`(AppService
facade)+ emit `quit_requested`;`_on_settings_changed` 调
`self.app.bus` 间接跨 mixin 但都走 facade — 跨 mixin attribute
由文件头 `# mypy: disable-error-code` 屏蔽。

`stats_changed.emit` / `last_message_received.emit` / 等跨 mixin
emit 由 Shiboken metaclass 透传,ChannelsMixin 业务
(`_on_channel_subscribed`)已 emit stats_changed,本 mixin 不
重复声明 — 描述符唯一在 SystemMixin,其它 mixin 跨访问。

MRO:`MonitorViewModel(ChannelsMixin, SystemMixin, ..., BusMixin,
QObject)`。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal

from tgmonitor.core.config import Settings
from tgmonitor.core.events import (
    ChannelSyncDone,
    ChannelSyncProgress,
    ConnectionStateChanged,
    ErrorOccurred,
    LoginStateChanged,
    QuitRequested,
    SettingsChanged,
)

if TYPE_CHECKING:
    from PySide6.QtCore import QObject

    _Base = QObject
else:
    _Base = object

log = logging.getLogger(__name__)


class SystemMixin(_Base):
    """系统事件 + 状态栏 4 signal — 2026-10-19 plan-batch9-γ。

    公开 API 1:1 保留原 `MonitorViewModel` system 域:
    - 1 method:`set_selected_channel`
    - 4 status bar signal:`selected_channel_changed` /
      `backend_label_changed` / `last_message_received` / `stats_changed`
    - 1 状态字段:`_message_count`(由 facade `__init__` 显式设)
    - 7 EventBus handler:LoginStateChanged / ConnectionStateChanged /
      QuitRequested / ErrorOccurred / SettingsChanged /
      ChannelSyncProgress / ChannelSyncDone
    """

    # 2026-10-01 v1.11.x 状态栏组件化:4 个新 Qt signal — payload 透传
    # (2026-10-19 plan-batch9-γ:从 facade 搬到本 mixin)。
    # - selected_channel_changed:payload str | None(LIVE 多选切换 / clear)
    # - backend_label_changed:payload str(DB=…,ObjectStore=…)
    # - last_message_received:payload datetime | None(每次 message_received
    #   末尾 emit,状态栏 _LastMessageLabel 取最新时间)
    # - stats_changed:payload tuple[int,int](监听频道数,消息总数)— 监听
    #   数从 channels_changed handler emit,消息总数从 message_received
    #   末尾累加 emit
    selected_channel_changed = Signal(object)
    backend_label_changed = Signal(str)
    last_message_received = Signal(object)
    stats_changed = Signal(object)

    _BUS_HANDLERS = {
        LoginStateChanged: "_on_login_state",
        ConnectionStateChanged: "_on_conn_state",
        QuitRequested: "_on_quit_requested",
        ErrorOccurred: "_on_error",
        SettingsChanged: "_on_settings_changed",
        ChannelSyncProgress: "_on_sync_progress",
        ChannelSyncDone: "_on_sync_done",
    }

    # ---- 7 EventBus handler ----

    async def _on_login_state(self, e: object) -> None:
        if not isinstance(e, LoginStateChanged):
            return
        self.login_state.emit(e.state)

    async def _on_conn_state(self, e: object) -> None:
        if not isinstance(e, ConnectionStateChanged):
            return
        self.conn_state.emit(e.state)

    async def _on_quit_requested(self, e: object) -> None:
        """2026-08-30 v1.5.0 PR #A4:tray menu「退出」/「暂停监听」事件。

        2026-09-03 v1.6.1:pause=True 现在真生效 — 调
        AppService.pause_monitor / resume_monitor(幂等),bus 后续发
        MonitoringPaused/Resumed 事件,VM 不重复 emit
        monitoring_paused/resumed signal(订阅方在 service 已发)。
        `pause=False` → emit Qt quit 信号,Qt 主循环走 aboutToQuit
        → _shutdown_then_quit → 真退出。
        """
        if not isinstance(e, QuitRequested):
            return
        if e.pause:
            if self.app.is_paused:
                log.info("QuitRequested(pause=True) — 当前已 paused,resume")
                await self.app.resume_monitor(source="tray")
            else:
                log.info("QuitRequested(pause=True) — 当前未 paused,pause")
                await self.app.pause_monitor(source="tray")
            return
        # 真退出 — emit 到 main_window 持有 qt_app 信号;此处不便
        # import qt_app,改发 signal 通知 main_window 真退出
        self.quit_requested.emit()

    async def _on_error(self, e: object) -> None:
        if not isinstance(e, ErrorOccurred):
            return
        self.error.emit(f"[{e.source}] {e.message}")

    async def _on_settings_changed(self, e: object) -> None:
        if not isinstance(e, SettingsChanged):
            return
        # events.py 里 SettingsChanged.new_settings: object | None(避免循环
        # import),这里 isinstance 窄化到 Settings,再用真字段拼 status 行。
        if not isinstance(e.new_settings, Settings):
            return
        new = e.new_settings
        backend_label = f"DB={new.db_backend.value}, ObjectStore={new.objectstore_backend.value}"
        self.settings_changed.emit(
            e.what,
            e.needs_relogin,
            e.needs_restart,
            backend_label,
        )
        # 2026-10-01 v1.11.x 状态栏组件化:状态栏 _BackendLabel 订阅本
        # 信号,自管更新,不再需要 MainWindow 转发。
        self.backend_label_changed.emit(backend_label)

    async def _on_sync_progress(self, e: object) -> None:
        if not isinstance(e, ChannelSyncProgress):
            return
        self.sync_progress.emit(e)

    async def _on_sync_done(self, e: object) -> None:
        if not isinstance(e, ChannelSyncDone):
            return
        self.sync_done.emit(e)

    # ---- 1 public method ----

    def set_selected_channel(self, name: str | None) -> None:
        """2026-10-01 v1.11.x 状态栏组件化:MainWindow 在 LIVE 多选 / 切换选中
        频道后调本方法,emit `selected_channel_changed` 让 status_bar
        `_SelectedChannelLabel` 更新显示。

        Args:
            name: 选中频道标题;None 表示清除选中(切走 / 用户按 Esc)。
        """
        self.selected_channel_changed.emit(name)
