"""MonitorViewModel — 把 EventBus 事件转 Qt signal(在 qasync 主线程安全更新 UI)。

2026-10-20 plan-batch9-ζ:5 mixin + BusMixin 拆分第六步 — export 域
下沉到 `_export.py:ExportMixin`(4 handler + 5 method + 6 signal +
`_export_task` 字段)。本 facade 多继承 `ChannelsMixin, SystemMixin,
MessagesMixin, MediaMixin, ExportMixin, BusMixin, QObject`;
`_wire_bus` 仅剩 `super().wire_bus()` 委派给 BusMixin 协议,自动挂
channels(2)+ system(7)+ messages(3)+ media(5)+ export(4)= **21 events**。

facade 残留(`C-η` 收口时下沉):
- 9 system signal:`login_state` / `conn_state` / `error` /
  `settings_changed` / `sync_progress` / `sync_done` /
  `quit_requested` / `monitoring_paused` / `monitoring_resumed`(全部
  emit 由 SystemMixin 7 handler 触发,signal 仍留 facade;C-η 再决定
  是否下沉到 SystemMixin 与 handler 同模块)
- `__init__` 显式 `known_channels` / `_export_task` / `_message_count`
  三字段初始化(facade 兜底)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import EventBus
from tgmonitor.ui.viewmodels._base import BusMixin
from tgmonitor.ui.viewmodels._channels import ChannelsMixin
from tgmonitor.ui.viewmodels._export import ExportMixin
from tgmonitor.ui.viewmodels._media import MediaMixin
from tgmonitor.ui.viewmodels._messages import MessagesMixin
from tgmonitor.ui.viewmodels._system import SystemMixin

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)


class MonitorViewModel(
    ChannelsMixin,
    SystemMixin,
    MessagesMixin,
    MediaMixin,
    ExportMixin,
    BusMixin,
    QObject,
):
    """EventBus → Qt signal 适配层(VM 模式)。

    # 唯一职责:把 core 内部事件转 Qt signal,在 qasync 主线程 / 同一 loop
    # 安全更新 UI。不持有任何业务状态 — 业务状态在 `MonitorService._whitelist`
    # 和 `AppService`。
    #
    # `known_channels` 是 UI 缓存(VM 唯一例外):订阅后收到 `ChannelSubscribed`
    # 就更新,VM 用它在下栏显示频道名 — 不重新查 storage。
    #
    # 直接 emit MessageDTO 本身(不是 asdict)。
    # 原因:dataclasses.asdict() 会把嵌套的 MediaDTO 也转成 dict,
    # MainWindow 收到后 `MessageDTO(**dto_dict)` 不递归构回 MediaDTO,
    # MessageView._format 取 `med.type` 崩溃。Signal(object) 让 Qt 承载
    # Python 对象本身,跨线程在 qasync 同一 loop 下安全。
    """

    # ---- 9 facade 残留 signal(由 SystemMixin 7 handler 触发 emit) ----
    # 2026-10-19 plan-batch9-γ:这 9 个 system signal **不**下沉到
    # SystemMixin — 它们是 facade-level "VM 顶层事件流",而
    # SystemMixin 是「状态栏 2 sub-component signal」
    # (`selected_channel_changed` / `backend_label_changed`)+ 7 handler
    # 内部消化逻辑。两个层语义不同,不合并。
    # 2026-10-19 plan-batch9-δ:C-δ 归位修正 `last_message_received` /
    # `stats_changed` 也属此类(emitted by handler 在其它 mixin)— 但已
    # 归位到 MessagesMixin(系统子组件 + 字段同 mixin 规则 vs handler
    # 在它 mixin 的字段写)。
    # 2026-10-20 plan-batch9-ζ:media / export / messages / channels 各自
    # mixin 持自己 signal,facade 只剩 9 个系统顶层 signal 与 login /
    # connection / error / settings / sync / quit / pause-resumed 相关。
    login_state = Signal(str)
    conn_state = Signal(
        str
    )  # TG 网络连接状态(waiting_for_network | connecting | updating | ready | unknown)
    error = Signal(str)
    settings_changed = Signal(
        str, bool, bool, str
    )  # (what, needs_relogin, needs_restart, backend_label)
    # 全量同步进度(sync dialog 订阅)
    sync_progress = Signal(object)  # ChannelSyncProgress
    sync_done = Signal(object)  # ChannelSyncDone(带 result)
    # 2026-08-30 v1.5.0 PR #A4:tray menu「退出」事件转发 — MainWindow
    # 接到 → qt_app.quit() 走 aboutToQuit → _shutdown_then_quit。
    # `pause=True` 路径不发此 signal(VM 内部 log 即可)。
    quit_requested = Signal()
    # 2026-09-03 v1.6.1:暂停 / 恢复监听 — payload str source("tray" 等)。
    # MainWindow 接到后:
    # - status bar label「⏸ 暂停」显示 / 隐藏
    # - window title 加 `(⏸ 暂停)` 后缀 / 取消
    # - tray icon 视觉由 tray_icon 自己监听 bus(MonitoringPaused/Resumed)
    monitoring_paused = Signal(str)
    monitoring_resumed = Signal(str)
    # 2026-10-19 plan-batch9-β:`~channels.py:ChannelsMixin` 已持
    # `channels_changed` signal(2 handler + 4 method 一起搬)。
    # 2026-10-19 plan-batch9-γ:`~system.py:SystemMixin` 已持
    # `selected_channel_changed` / `backend_label_changed` 2 个系统子组件
    # 状态栏 signal。
    # 2026-10-19 plan-batch9-δ:`~messages.py:MessagesMixin` 已持 messages
    # 6 signal(`message_received` / `message_edited` /
    # `message_interactions_changed` / `message_search_results` /
    # `last_message_received` / `stats_changed`)。
    # 2026-10-20 plan-batch9-ε:`~media.py:MediaMixin` 已持 media 10 signal。
    # 2026-10-20 plan-batch9-ζ:`~export.py:ExportMixin` 已持 export 6 signal。

    def __init__(
        self,
        app: AppService,
        monitor: MonitorService,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """存 3 个子系统引用 + 立即订阅 EventBus 所有相关事件。

        2026-10-20 plan-batch9-ζ:`_wire_bus()` 已简化为 1 行
        `super().wire_bus()`,BusMixin 协议接管 21 events 自动挂
        (channels 2 + system 7 + messages 3 + media 5 + export 4)。

        字段初始化(plan §8 风险 #2 「单点 init 收敛」惯例):
        - `known_channels: dict` 由 ChannelsMixin 用,filled by ChannelSubscribed
        - `_export_task: asyncio.Future | None` 由 ExportMixin 持,
          `start_export` 写入 / `_on_export_done` 清空
        - `_message_count: int = 0` 由 MessagesMixin `_on_message_received`
          累加(emits `last_message_received` / `stats_changed` 信号)

        `BusMixin.__init__` 接管所有字段初始化待 `C-η` 收口;目前 facade
        仍兜底,public 测试构造走 facade `__init__` 不变。
        """
        super().__init__()
        self.app = app
        self.monitor = monitor
        self.loop = loop
        self.known_channels: dict[int, ChannelDTO] = {}
        self._export_task: asyncio.Future[None] | None = None
        self._message_count = 0
        self._wire_bus()

    def _wire_bus(self) -> None:
        """2026-10-20 plan-batch9-ζ:facade 仅剩 1 行 `super().wire_bus()`。

        BusMixin 协议走 `type(self).__mro__` walk 累加 5 mixin 的
        `_BUS_HANDLERS`,自动挂 21 events(ChannelsMixin 2 +
        SystemMixin 7 + MessagesMixin 3 + MediaMixin 5 + ExportMixin 4)。
        """
        _b: EventBus = self.app.bus
        super().wire_bus()

    # ---- 已下沉 method 索引 ----
    # 2026-10-19 plan-batch9-β:`~channels.py:ChannelsMixin`:4 method
    # (`bootstrap_ui` / `refresh_subscribed_channels` / `subscribe_channel` /
    # `unsubscribe_channel`)+ 2 handler + `channels_changed` signal +
    # `known_channels` 字段。
    # 2026-10-19 plan-batch9-γ:`~system.py:SystemMixin`:`set_selected_channel`
    # + 7 handler + 2 statusbar signal。
    # 2026-10-19 plan-batch9-δ:`~messages.py:MessagesMixin`:
    # `load_recent_messages` / `search_messages` 2 method + 3 handler + 6
    # signal + `_message_count` 字段(facade 兜底)。
    # 2026-10-20 plan-batch9-ε:`~media.py:MediaMixin`:5 handler + 10 method
    # + 10 signal(最大块)。
    # 2026-10-20 plan-batch9-ζ:`~export.py:ExportMixin`:4 handler + 5 method
    # + 6 signal + `_export_task` 字段(facade 兜底)。

    # public 9 signal declaration 已在 class body 上方(为 `登录/连接/错误/
    # 设置/同步/退出/暂停恢复` facade-level 事件转发),全部由 SystemMixin
    # 7 handler(在 `_system.py`)内部 `self.<signal>.emit(...)` 触发 emit。

    # public 9 method(`load_recent_messages` / `search_messages` /
    # `bootstrap_ui` / `refresh_subscribed_channels` / `subscribe_channel` /
    # `unsubscribe_channel` / `set_selected_channel` /
    # `load_media_list` ... 一共 17 method + 5 export method)全部下沉到
    # 5 mixin 中,facade 不再有任何 method body。
