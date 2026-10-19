# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""SystemMixin — 状态栏(系统子组件)+ 系统事件 handler 集合 — 2026-10-19 plan-batch9-γ+δ。

`MonitorViewModel` 拆 5 mixin 第三步(2026-10-19
plan-batch9-γ + 2026-10-19 plan-batch9-δ 归位修正):system 域 =
7 handler + 2 status bar signal + 1 method。

- handler(7):`_on_login_state` / `_on_conn_state` /
  `_on_quit_requested`(含 pause 调 `app.pause_monitor` /
  `resume_monitor`)/ `_on_error` / `_on_settings_changed`(emit
  `settings_changed` + `backend_label_changed`)/ `_on_sync_progress` /
  `_on_sync_done`
- status bar signal(2 — 2026-10-19 plan-batch9-δ 归位修正):
  `selected_channel_changed` / `backend_label_changed`
  — 2026-10-01 v1.11.x 状态栏组件化,这 2 个跟系统状态(DB、ObjectStore
  后端)绑定,留本 mixin
- method:`set_selected_channel`(emit `selected_channel_changed` 给
  `_SelectedChannelLabel`)

**归位修正(2026-10-19 plan-batch9-δ)**:`last_message_received` /
`stats_changed` 2 个 signal 原 C-γ 放在本 mixin(`"状态栏组件化"`
语义统一),C-δ 创建 `_messages.py` 时发现违反 plan §8 风险 #3 原则
**字段与 emit 该字段的 signal 同 mixin 持有**(`_message_count` 字段
+ 这 2 signal 都由 `MessageReceived` 唯一触发),从本 mixin 搬出到
`_messages.py:MessagesMixin`。`_message_count: int = 0` 字段也
跟随归位(实际值仍由 facade `__init__` 显式设,本 mixin 不持字段,
仅语义归类)。

跨 mixin access:`_on_login_state` 不跨;`_on_quit_requested` 调
`self.app.is_paused / pause_monitor / resume_monitor`(AppService
facade)+ emit `quit_requested`;`_on_settings_changed` emit
`settings_changed` + `backend_label_changed`(本 mixin 2 signal)— 跨
mixin attribute 由文件头 `# mypy: disable-error-code` 屏蔽。

`stats_changed` / `last_message_received` 跨 mixin emit 仍合法:由
MessagesMixin 持有 signal,`ChannelsMixin._on_channel_subscribed`
跨 mixin emit `self.stats_changed.emit(...)` 由 Shiboken metaclass
聚合 Signal 描述符透传。

MRO:`MonitorViewModel(ChannelsMixin, SystemMixin, MessagesMixin,
BusMixin, QObject)` — MessagesMixin 在 System 之后,BusMixin 末。
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
    """系统事件 + 状态栏 2 signal(系统子组件) — 2026-10-19 plan-batch9-γ+δ。

    公开 API 1:1 保留原 `MonitorViewModel` system 域:
    - 1 method:`set_selected_channel`
    - 2 status bar signal(系统子组件):`selected_channel_changed` /
      `backend_label_changed`
    - 2026-10-19 plan-batch9-δ 归位:`last_message_received` /
      `stats_changed` 2 signal 已搬到 MessagesMixin(语义由
      MessageReceived 触发)
    - 7 EventBus handler:LoginStateChanged / ConnectionStateChanged /
      QuitRequested / ErrorOccurred / SettingsChanged /
      ChannelSyncProgress / ChannelSyncDone
    """

    # 2026-10-01 v1.11.x 状态栏组件化:2 个系统子组件 Qt signal — payload 透传
    # (2026-10-19 plan-batch9-γ:从 facade 搬到本 mixin)。
    # - selected_channel_changed:payload str | None(LIVE 多选切换 / clear)
    # - backend_label_changed:payload str(DB=…,ObjectStore=…)
    # `last_message_received` / `stats_changed` 2 个 messages 域 signal
    # 2026-10-19 plan-batch9-δ 归位到 `_messages.py:MessagesMixin`(由
    # MessageReceived 唯一触发,与 `_message_count` 字段同 mixin)。
    selected_channel_changed = Signal(object)
    backend_label_changed = Signal(str)

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
