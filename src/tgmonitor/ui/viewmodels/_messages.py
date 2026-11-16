# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""MessagesMixin — 消息域 handler + method 集合 — 2026-10-19 plan-batch9-δ。

`MonitorViewModel` 拆 5 mixin + BusMixin 第四步(2026-10-19
plan-batch9-δ):messages 域 = 3 handler + 1 method + 5 signal。

2026-11-16 v2.0.2:移除 `search_messages` method 与 `message_search_results`
signal(顶部搜索功能区整体移除)。`load_recent_messages` limit 200→100。

设计归位(2026-10-19 plan-batch9-δ):
- 在 C-γ 阶段,`_message_count` / `last_message_received` / `stats_changed`
  与 4 个状态栏 signal 一起放进了 SystemMixin(归到"状态栏组件化"语义)。
- 但 plan §8 风险 #3 设计原则:**字段与 emit 该字段的 signal 同 mixin 持有**
  (`_message_count` + `last_message_received` + `stats_changed` 都由
  `MessageReceived` 唯一触发)。
- 因此 C-δ 把这 3 个从 SystemMixin 搬回 MessagesMixin(归位到 messages 域)。

内容清单:
- 3 handler:`_on_message_received` / `_on_message_edited` /
  `_on_message_interactions_changed`
  - `_on_message_received` 末尾:`self._message_count += 1` + emit
    `last_message_received` + emit `stats_changed`(全是本 mixin 内 signal)
- 1 method:`load_recent_messages`
- 5 signal:
  - `message_received` / `message_edited` / `message_interactions_changed`
    (3 消息核心 signal)
  - `last_message_received`(状态栏 _LastMessageLabel 订阅)
  - `stats_changed`(状态栏 _StatsLabel 订阅;跨 mixin 由
    `_channels.py:ChannelsMixin._on_channel_subscribed` emit)

跨 mixin access:
- `_on_message_received` 内部:`self._message_count += 1` 是本 mixin 字段;
  emit `self.last_message_received` / `self.stats_changed` 是本 mixin signal;
  emit `self.message_received` 也本 mixin — 跨 mixin 通信 0。
- `_on_message_received` 外部 emit 触发方:`ChannelsMixin._on_channel_subscribed`
  也 emit `self.stats_changed.emit((len(self.known_channels),
  self._message_count))` — 跨 mixin emit(ChannelsMixin → MessagesMixin
  signal),由 Shiboken metaclass 透传,mypy 已被文件头 disable-error-code
  屏蔽。

字段保留:`_message_count: int = 0` 由 facade `__init__` 显式
`self._message_count = 0` 设(2026-10-01 v1.11.x,plan §8 风险 #2
"单点 init 收敛"惯例 — BusMixin.__init__ 接管留 C-η),本 mixin 不写
`__init__`。

MRO:`MonitorViewModel(ChannelsMixin, SystemMixin, MessagesMixin,
BusMixin, QObject)` — MessagesMixin 顺序在 System 之后,BusMixin 末
(QObject 兜底 super().__init__)。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal

from tgmonitor.core.events import (
    MessageEdited,
    MessageInteractionsChanged,
    MessageReceived,
)
from tgmonitor.ui.async_bridge import run_coro

if TYPE_CHECKING:
    from PySide6.QtCore import QObject

    _Base = QObject
else:
    _Base = object

log = logging.getLogger(__name__)


class MessagesMixin(_Base):
    """消息域 — 2026-10-19 plan-batch9-δ。

    公开 API:
    - 1 method:`load_recent_messages`
    - 5 signal:`message_received` / `message_edited` /
      `message_interactions_changed` / `last_message_received` / `stats_changed`
    - 1 状态字段:`_message_count: int = 0`(facade `__init__` 显式设,
      本 mixin 不写 init)
    - 3 EventBus handler:`MessageReceived` / `MessageEdited` /
      `MessageInteractionsChanged`
    """

    # 2026-08-24:新消息事件。直接 emit MessageDTO 本身(不是 asdict)。
    # 原因:dataclasses.asdict() 会把嵌套的 MediaDTO 也转成 dict,
    # MainWindow 收到后 `MessageDTO(**dto_dict)` 不递归构回 MediaDTO,
    # MessageView._format 取 `med.type` 崩溃。Signal(object) 让 Qt 承载
    # Python 对象本身,跨线程在 qasync 同一 loop 下安全。
    message_received = Signal(object)
    # 2026-08-24:编辑事件 MessageDTO
    message_edited = Signal(object)
    # 2026-09-11 v1.7.4:reactions 增量推送 — payload `MessageInteractionsChanged`。
    # TDLib `updateMessageReactions` 是 bots-only,user client 不收 — 依赖
    # `updateMessageInteractionInfo`(`MessageInteractionsChanged.reactions`),
    # 实时性受服务端策略限制。MainWindow 接到 → 调 LIVE 行
    # `refresh_reactions`(局部更新,不重建列表)。
    message_interactions_changed = Signal(object)
    # 2026-10-01 v1.11.x 状态栏组件化:
    # - last_message_received:payload datetime | None(每次 message_received
    #   末尾 emit,状态栏 _LastMessageLabel 取最新时间)
    # - stats_changed:payload tuple[int,int](监听频道数,消息总数)—
    #   监听数从 channels_changed handler emit,消息总数从 message_received
    #   末尾累加 emit
    # 2026-10-19 plan-batch9-δ:从 SystemMixin 归位到本 mixin(字段同 mixin 设计原则)。
    last_message_received = Signal(object)
    stats_changed = Signal(object)

    # 2026-10-19 plan-batch9-δ + 2026-10-20 plan-batch9-η:facade 字段
    # 兜底初值由 `BusMixin.__init__` 统一接管,但 mixin 字段必须在
    # class body 声明,mypy 类型推断才不会跨 mixin 错位
    # (plan §8 #3 「字段同 mixin 持有」)。
    _message_count: int = 0

    _BUS_HANDLERS = {
        MessageReceived: "_on_message_received",
        MessageEdited: "_on_message_edited",
        MessageInteractionsChanged: "_on_message_interactions_changed",
    }

    # ---- 3 EventBus handler ----

    async def _on_message_received(self, e: object) -> None:
        if not isinstance(e, MessageReceived) or e.message is None:
            return
        # 直接 emit MessageDTO — 不要 asdict,会丢嵌套 MediaDTO 类型
        self.message_received.emit(e.message)
        # 2026-10-01 v1.11.x 状态栏组件化:累计消息数 + emit 状态栏 2 signal
        # (last_message_received / stats_changed)。状态栏 _StatsLabel /
        # _LastMessageLabel 自己渲染;MainWindow 不再关心。
        self._message_count += 1
        self.last_message_received.emit(e.message.date)
        self.stats_changed.emit((len(self.known_channels), self._message_count))

    async def _on_message_edited(self, e: object) -> None:
        """2026-08-24:Telegram 推送的编辑事件 → 转发 UI。

        UI 通过 message_edited 信号 → MessageView.replace_message 找到现有
        cell 重渲,不增删 row。
        """
        if not isinstance(e, MessageEdited) or e.message is None:
            return
        self.message_edited.emit(e.message)

    async def _on_message_interactions_changed(self, e: object) -> None:
        """2026-09-11 v1.7.4:Telegram 推送的 reactions / views 变化。

        转发到 MainWindow,后者区分 reactions / views 分别走局部刷新路径。
        详见 `message_interactions_changed` signal docstring — TDLib 限制:
        `updateMessageReactions` bots-only,user client 不收。
        """
        if not isinstance(e, MessageInteractionsChanged):
            return
        self.message_interactions_changed.emit(e)

    # ---- 2 public method ----

    def load_recent_messages(self) -> None:
        """启动时拉最近 100 条已订阅频道消息 → emit message_received(填充 LIVE view)。

        2026-11-16 v2.0.2:LIVE 页收敛为只读监听状态看板 —— 展示"当前已监听频道
        最近落地的 100 条消息"。`MessageReceived` 在 `storage.save_message()`
        之后发布(`core/monitor/service.py`),故本方法拉到的都是已落地消息。
        视图容量上限同步收敛为 `MessageListModel.MAX_ITEMS=100`。
        """

        async def _go() -> None:
            msgs = await self.app.messages.list_messages(limit=100)
            for m in msgs:
                self.message_received.emit(m)

        run_coro(self.loop, _go(), error_label="load_recent_messages")
