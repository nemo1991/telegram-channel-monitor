"""MonitorViewModel — EventBus → Qt signal 适配层(VM 模式)入口壳。

2026-10-20 plan-batch9-η:5 mixin + BusMixin 拆分收口 — `monitor_vm.py`
从 839 → ~50 LOC,只剩类声明 + 多继承(`BusMixin.__init__` 自动接管
`__init__` + 3 字段初始化 + `wire_bus()` 调 21 events)。

facade 不再持:
- 9 system signal(`login_state` / `conn_state` / `error` /
  `settings_changed` / `sync_progress` / `sync_done` / `quit_requested` /
  `monitoring_paused` / `monitoring_resumed`)— 已下沉到 `_system.py`
  (plan §8 #3 「字段与 emit 该字段的 signal 同 mixin 持有」)
- `__init__` — `BusMixin.__init__` 接管:`super().__init__()` →
  赋 3 引用(`app` / `monitor` / `loop`)→ `self.wire_bus()` 自动挂
  21 events(channels 2 + system 7 + messages 3 + media 5 + export 4)
- `_wire_bus` — 1 行 `super().wire_bus()` 委派也由 `BusMixin.__init__`
  内部 `self.wire_bus()` 调,facade 不需要再声明
- 3 字段初始化:`known_channels` / `_export_task` / `_message_count` —
  改由各自 mixin 类体声明提供 default 初值(ChannelsMixin 字典
  / ExportMixin None / MessagesMixin 0),BusMixin.__init__ 不重复
  赋值(避免 facade init 跨 mixin 写字段违反「字段同 mixin 持有」)

公开 API 1:1 保留:33 Signal(5 mixin 各自类体声明 + 多继承合入
facade)+ 17 public method(5 mixin 提供)+ 3 状态字段;`main_window +
18 controller + 6 dialog widget` 调用方 0 改动,`tests/fixtures/_fake_app.py`
+ 6 个直接构造 VM 的 test 文件签名兼容验证。
"""

from __future__ import annotations

from PySide6.QtCore import QObject

from tgmonitor.ui.viewmodels._base import BusMixin
from tgmonitor.ui.viewmodels._channels import ChannelsMixin
from tgmonitor.ui.viewmodels._export import ExportMixin
from tgmonitor.ui.viewmodels._media import MediaMixin
from tgmonitor.ui.viewmodels._messages import MessagesMixin
from tgmonitor.ui.viewmodels._system import SystemMixin


class MonitorViewModel(
    ChannelsMixin,
    SystemMixin,
    MessagesMixin,
    MediaMixin,
    ExportMixin,
    BusMixin,
    QObject,
):
    """EventBus → Qt signal 适配层(VM 模式)入口壳 — 2026-10-20 plan-batch9-η。

    # 唯一职责:把 core 内部事件转 Qt signal,在 qasync 主线程 / 同一 loop
    # 安全更新 UI。不持有任何业务状态 — 业务状态在 `MonitorService._whitelist`
    # 和 `AppService`。
    #
    # `known_channels` 是 UI 缓存(VM 唯一例外,ChannelsMixin 持有):订阅后
    # 收到 `ChannelSubscribed` 就更新,VM 用它在下栏显示频道名 — 不重新查
    # storage。
    #
    # 直接 emit MessageDTO 本身(不是 asdict)。
    # 原因:dataclasses.asdict() 会把嵌套的 MediaDTO 也转成 dict,
    # MainWindow 收到后 `MessageDTO(**dto_dict)` 不递归构回 MediaDTO,
    # MessageView._format 取 `med.type` 崩溃。Signal(object) 让 Qt 承载
    # Python 对象本身,跨线程在 qasync 同一 loop 下安全。
    #
    # 2026-10-20 plan-batch9-η:`__init__` 由 `BusMixin.__init__` 接管,
    # facade 类体不再声明。3 引用(`app` / `monitor` / `loop`)+ 3 字段
    # (`known_channels` / `_export_task` / `_message_count`)由各自 mixin
    # 类体 default 初值兜底。
    #
    # 33 Signal 分布:
    # - ChannelsMixin(1):`channels_changed`
    # - SystemMixin(11):9 facade-level(`login_state` / `conn_state` /
    #   `error` / `settings_changed` / `sync_progress` / `sync_done` /
    #   `quit_requested` / `monitoring_paused` / `monitoring_resumed`)
    #   + 2 statusbar sub-component(`selected_channel_changed` /
    #   `backend_label_changed`)
    # - MessagesMixin(6):`message_received` / `message_edited` /
    #   `message_interactions_changed` / `message_search_results` /
    #   `last_message_received` / `stats_changed`
    # - MediaMixin(10):media 域全部 signal
    # - ExportMixin(6):export 域全部 signal
    #
    # 17 public method 分布:
    # - ChannelsMixin(4):`bootstrap_ui` / `refresh_subscribed_channels` /
    #   `subscribe_channel` / `unsubscribe_channel`
    # - SystemMixin(1):`set_selected_channel`
    # - MessagesMixin(2):`load_recent_messages` / `search_messages`
    # - MediaMixin(10):media 域 10 method
    # - ExportMixin(5):`start_export` / `cancel_current_export` /
    #   `cancel_current_batch` / `export_media_list` / `export_zip`
    #
    # 3 状态字段:
    # - `known_channels: dict[int, ChannelDTO]` — ChannelsMixin 类体声明,
    #   ChannelSubscribed / ChannelUnsubscribed handler 维护
    # - `_export_task: asyncio.Future[None] | None` — ExportMixin 类体声明,
    #   start_export 写入 / _on_export_done 清空
    # - `_message_count: int` — MessagesMixin 类体声明, _on_message_received
    #   累加
    """
