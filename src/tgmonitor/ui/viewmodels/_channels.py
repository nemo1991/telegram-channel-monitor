# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""ChannelsMixin — 频道域 handler + method 集合 — 2026-10-19 plan-batch9-β。

`MonitorViewModel` 拆 5 mixin + BusMixin 第二步(2026-10-19
plan-batch9-β):channels 域 = 2 handler + 4 method + 1 signal +
`known_channels` 字段。

- handler:`_on_channel_subscribed` / `_on_channel_unsubscribed`
- method:`bootstrap_ui` / `refresh_subscribed_channels` /
  `subscribe_channel` / `unsubscribe_channel`
- signal:`channels_changed`
- 状态:`known_channels: dict[int, ChannelDTO]`(public,被
  `main_window.py:755` `self._vm.known_channels` 读,不能改命名 /
  访问性)— 由 monitor_vm `__init__` 显式设 `{}`,本 mixin 不写
  `__init__` 避免与 facade 冲突(2026-10-19 plan-batch9 §8 风险
  #2 缓解:仅 BusMixin 的 `__init__` 显式 `super().__init__()` 收口)

跨 mixin access:handler 调 `self.app.add_to_whitelist` /
`self.remove_from_whitelist`(AppService facade)+ `self.stats_changed.emit`
(SystemMixin 字段)— 跨 mixin attribute / signal 由 Shiboken
metaclass 透传,mypy 已被文件头 disable-error-code 屏蔽。

MRO 顺序:plan-batch9 §2 `MonitorViewModel(ChannelsMixin, ...,
BusMixin, QObject)` — ChannelsMixin 在前(只读其它 mixin 字段
+ signal emit),BusMixin 在末(它要 `super().__init__()` 走
QObject)。本 mixin 不写 `__init__`。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import ChannelSubscribed, ChannelUnsubscribed
from tgmonitor.ui.async_bridge import run_coro

if TYPE_CHECKING:
    # 2026-10-19 plan-batch9-β:让 mypy 视 ChannelsMixin 为 QObject 间接
    # 子类,Signal 描述符绑定 OK。运行时通过 MonitorViewModel 多继承
    # (MRO 末 BusMixin → QObject)兜底 — 不在类体显式继承,避免多
    # 继承下 QObject 重复。
    from PySide6.QtCore import QObject

    _Base = QObject
else:
    _Base = object

log = logging.getLogger(__name__)


class ChannelsMixin(_Base):
    """频道域 — 2026-10-19 plan-batch9-β。

    公开 API 1:1 保留原 `MonitorViewModel` 频道域:
    - 4 method:`bootstrap_ui` / `refresh_subscribed_channels` /
      `subscribe_channel` / `unsubscribe_channel`
    - 1 signal:`channels_changed`
    - 1 状态字段:`known_channels: dict[int, ChannelDTO]`(由
      monitor_vm `__init__` 设,本 mixin 不写 init)
    - 2 EventBus handler:ChannelSubscribed / ChannelUnsubscribed
    """

    channels_changed = Signal()

    # 2026-10-19 plan-batch9-β:handler 自动挂 — BusMixin.wire_bus()
    # 走 MRO walk 累加本 dict,`getattr(self, name)` 拿 bound method
    # 给 EventBus.subscribe。handler 名错 → AttributeError 在
    # `wire_bus()` 调时即失败。
    _BUS_HANDLERS = {
        ChannelSubscribed: "_on_channel_subscribed",
        ChannelUnsubscribed: "_on_channel_unsubscribed",
    }

    # ---- 2 EventBus handler ----

    async def _on_channel_subscribed(self, e: object) -> None:
        if not isinstance(e, ChannelSubscribed) or e.channel is None:
            return
        self.known_channels[e.channel.id] = e.channel
        # 2026-10-15 plan-batch6-2 副产物:走 AppService facade 转发
        # 到 MonitorService 内部 WhitelistMixin(sync),不再绕过 facade
        # 直调 `self.monitor.add_to_whitelist(cid)`。
        self.app.add_to_whitelist(e.channel.id)
        self.channels_changed.emit()
        # 2026-10-01 v1.11.x 状态栏组件化:状态栏 _StatsLabel 订阅
        # stats_changed(SystemMixin 持有)→ 跨 mixin emit。
        self.stats_changed.emit((len(self.known_channels), self._message_count))

    async def _on_channel_unsubscribed(self, e: object) -> None:
        if not isinstance(e, ChannelUnsubscribed):
            return
        # 2026-10-15 plan-batch6-2 副产物:同上,走 facade。
        self.app.remove_from_whitelist(e.channel_id)
        # 2026-09-29:Media Manager combo 走 storage 真理,unsub 后
        # 立即从 known_channels 清掉 — 下次 channels_changed →
        # set_known_channels 不再显示这个频道。不必等下次
        # refresh_subscribed_channels 同步。
        self.known_channels.pop(e.channel_id, None)
        self.channels_changed.emit()
        # 跨 mixin emit(SystemMixin stats_changed signal)
        self.stats_changed.emit((len(self.known_channels), self._message_count))

    # ---- 4 public method ----

    def bootstrap_ui(self) -> None:
        """MainWindow 构造后调一次:拉一次 subscribed 列表 + 通知 UI 刷新下栏。

        2026-08-30 沿用 — 为什么需要:
        - `bootstrap()` 同步了 `_subscribed` 到内存,但 VM 不知道。
        - VM 的 `known_channels` 只在 `refresh_subscribed_channels`
          或 `ChannelSubscribed` 事件后才填充,启动时为空 →
          `_refresh_state` 算 `subscribed` 时筛不出任何行 → 下栏
          一直空。
        - 这里的 `refresh_subscribed_channels` 同时也补了已知
          频道的元数据(title / username),下栏才能显示频道名
          而不是 "频道 -1001xxx"。

        已监听的 id 列表(monitor._whitelist)在 `app._setup_async`
        里已经从 storage 读回并 set,这里只负责把 DTO 拉回来
        填 `known_channels`,然后 emit `channels_changed` 让 UI
        算交集并刷新。
        """
        self.refresh_subscribed_channels()

    def refresh_subscribed_channels(self) -> None:
        """后台从 storage 拉已订阅频道 → 填 known_channels → emit channels_changed。

        2026-09-29:从 `list_joined_channels`(TDLib getChats,实时但
        跟真理漂移)改为 `list_subscribed_channels`(storage 落库
        数据,跟 `MonitorService._whitelist` 的真理对齐)。

        旧设计问题:用户 join 了 1000 个频道但只白名单 5 个时,
        Media Manager「全部频道」下拉会出现 1000 项噪音;用户
        实际只关心自己订阅的。新设计:跟 monitor 白名单完全
        一致 —— 已订阅 → 显示;退订 → 不显示(即时,
        ChannelUnsubscribed 事件里同步 del);启动 → 从 storage
        立刻拉回(不等 TDLib state)。
        """

        async def _go() -> None:
            chs = await self.app.list_subscribed_channels()
            for ch in chs:
                self.known_channels[ch.id] = ch
            self.channels_changed.emit()
            # 2026-10-01 v1.11.x 状态栏组件化:启动后立即 emit
            # stats_changed 让状态栏 _StatsLabel 拿到首屏已订阅
            # 频道数。
            self.stats_changed.emit(
                (len(self.known_channels), self._message_count),
            )

        run_coro(self.loop, _go(), error_label="refresh_channels")

    def subscribe_channel(self, ch: ChannelDTO) -> None:
        """订阅频道 — 后台 fire `app.subscribe_channel`(同步入 storage + 发事件)。"""
        run_coro(
            self.loop,
            self.app.subscribe_channel(ch),
            error_label="subscribe_channel",
        )

    def unsubscribe_channel(self, channel_id: int) -> None:
        """退订频道 — 后台 fire `app.unsubscribe_channel`(关订阅标志,保留历史)。"""

        async def _go() -> None:
            await self.app.unsubscribe_channel(channel_id)

        run_coro(self.loop, _go(), error_label="unsubscribe_channel")
