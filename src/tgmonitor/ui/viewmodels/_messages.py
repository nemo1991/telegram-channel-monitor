# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""MessagesMixin — 消息域 handler + method 集合 — 2026-10-19 plan-batch9-δ。

`MonitorViewModel` 拆 5 mixin + BusMixin 第四步(2026-10-19
plan-batch9-δ):messages 域 = 3 handler + 2 method + 6 signal。

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
- 2 method:`load_recent_messages` / `search_messages`
- 6 signal:
  - `message_received` / `message_edited` / `message_interactions_changed`
    (3 消息核心 signal)
  - `message_search_results`(搜索结果透传给 LIVE view)
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
from datetime import datetime
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

    公开 API 1:1 保留原 `MonitorViewModel` messages 域:
    - 2 method:`load_recent_messages` / `search_messages`
    - 6 signal:`message_received` / `message_edited` /
      `message_interactions_changed` / `message_search_results` /
      `last_message_received` / `stats_changed`
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
    # 2026-09-02 v1.5.2 PR #B5:VM 搜索结果 — payload list[MessageDTO]。
    # MainWindow 接到 → live_view.set_messages(messages) 批量替换。
    # 空 list = 清空视图(query 没命中或被 clear)。
    message_search_results = Signal(object)
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
        """2026-08-24:TDLib updateMessageContent 来的编辑事件 → 转发 UI。

        UI 通过 message_edited 信号 → MessageView.replace_message 找到现有
        cell 重渲,不增删 row。
        """
        if not isinstance(e, MessageEdited) or e.message is None:
            return
        self.message_edited.emit(e.message)

    async def _on_message_interactions_changed(self, e: object) -> None:
        """2026-09-11 v1.7.4:TDLib `updateMessageInteractionInfo` 来的 reactions / views 变化。

        转发到 MainWindow,后者区分 reactions / views 分别走局部刷新路径。
        详见 `message_interactions_changed` signal docstring — TDLib 限制:
        `updateMessageReactions` bots-only,user client 不收。
        """
        if not isinstance(e, MessageInteractionsChanged):
            return
        self.message_interactions_changed.emit(e)

    # ---- 2 public method ----

    def load_recent_messages(self) -> None:
        """启动时拉最近 200 条已订阅频道消息 → emit message_received(填充 LIVE view)。"""

        async def _go() -> None:
            msgs = await self.app.list_messages(limit=200)
            for m in msgs:
                self.message_received.emit(m)

        run_coro(self.loop, _go(), error_label="load_recent_messages")

    def search_messages(
        self,
        text: str = "",
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 200,
        channel_ids: list[int] | None = None,
        scope: str = "subscribed",
        favorite_only: bool = False,
        tag_only: bool = False,
        pinned_only: bool = False,
    ) -> None:
        """2026-09-02 v1.5.2 PR #B5 + 2026-09-03 v1.5.3 PR #D2 +
        2026-09-14 v1.7.5 PR #8:VM 异步拉搜索结果 → emit `message_search_results`。

        行为契约:
        - `text=""` 且 `date_from/to` 全 None → 无意义查询,emit `[]`(清空视图)。
          MainWindow 用此判定"清空搜索框 = 恢复 LIVE 流(空 search 视为清空)"。
        - `channel_ids=None` + `scope="subscribed"`(默认)→ 走已订阅频道
          (与 v1.5.2 行为一致)。
        - `channel_ids=None` + `scope="all"`(PR #D2)→ 走
          `include_unsubscribed=True`,跨频道聚合(含已退订但 storage 仍
          有历史消息的频道)。
        - `channel_ids=...` 显式传 → 忽略 scope(精细控制场景)。
        - `limit` 默认 200(用户决定保留 v1.5.1 默认值,不 bump)。
        - 2026-09-14 v1.7.5 PR #8:`favorite_only` / `tag_only` / `pinned_only`
          (SearchBar ★/🏷/📌 3 toggle),透传 `app.list_messages` → 存储层
          SQL / Mongo 子句,AND 语义任一 True 必须命中。

        走 `run_coro` 后台 fire;`message_search_results` emit 在 qasync 主
        线程 loop,Qt signal 安全 → `live_view.set_messages(...)` 主线程 op。

        UI 端双过滤(在 MainWindow._on_search_changed):text 一变即时
        `live_view.set_filter(text)` 快过滤 LIVE 流;同时启 300ms debounce
        调本方法 — search_messages 结果到时 `set_messages` 替换视图
        (search 命中的命中子集)。两者不冲突:即时快过滤不重 IO,异步拉
        替换视图是「权威」。
        """

        async def _go() -> None:
            include_unsub = scope == "all" and channel_ids is None
            msgs = await self.app.list_messages(
                channel_ids=channel_ids,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
                search=text,
                include_unsubscribed=include_unsub,
                favorite_only=favorite_only,
                tag_only=tag_only,
                pinned_only=pinned_only,
            )
            self.message_search_results.emit(msgs)

        run_coro(self.loop, _go(), error_label="search_messages")
