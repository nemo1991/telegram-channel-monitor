"""LIVE 流消息事件 controller — 4 handler。

2026-10-04 plan 阶段 3.3:从 `MainWindow` 抽 4 个 VM/EventBus 入口的
LIVE 流更新 handler。沿用 `SearchController` / `MediaActionsController` /
`LiveSelectionController` 模板(plain class + `MainWindowCtx`)。

信号契约:
- `vm.message_received` → `on_message_received`:set_channel_titles +
  append + throttle_activity(1.5s/次,大流量限频)
- `vm.message_edited` → `on_message_edited`:整条 cell 重渲(replace_message)
- `vm.message_interactions_changed` → `on_message_interactions_changed`:
  isinstance 守门 + LIVE 行 reactions 局部刷新 + 详情面板同步
- `EventBus.subscribe(MessageDeleted, ...)` → `on_bus_message_deleted`:
  `async`(跟 bus publish 协程对齐),remove_row + activity toast

`_on_bus_message_deleted` 在 MainWindow 原生就是 `async def`(订阅者签
  名走 `Subscriber = Callable[[Event], Awaitable[None] | None]`,bus
  对 await / sync 都兼容)。Controller 保持 async 不破坏订阅。
"""

from __future__ import annotations

from tgmonitor.core.dto import MessageDTO
from tgmonitor.core.events import MessageDeleted, MessageInteractionsChanged
from tgmonitor.ui.controllers._base import MainWindowCtx


class MessageStreamController:
    """LIVE 流消息事件 — VM signal + EventBus 入口 4 handler。"""

    def __init__(self, ctx: MainWindowCtx) -> None:
        self._ctx = ctx

    def on_message_received(self, m: MessageDTO) -> None:
        """VM:MonitorViewModel 收到新消息已落库 → LIVE 视图追加 + 标题同步。

        活动指示器:限频 1.5s/次,避免大流量刷屏(`throttle_activity` 内部
        走 key+min_interval_ms 节流,单 key 在窗口内只更新一次 label)。
        """
        self._ctx.live_view.set_channel_titles(
            {cid: ch.title for cid, ch in self._ctx.vm.known_channels.items()}
        )
        self._ctx.live_view.append(m)
        ch_title = self._ctx.vm.known_channels.get(m.channel_id)
        title = ch_title.title if ch_title else f"#{m.channel_id}"
        self._ctx.status_bar.throttle_activity(
            "message_received",
            f"+1 {title}",
            min_interval_ms=1500,
            timeout_ms=1200,
        )

    def on_message_edited(self, m: MessageDTO) -> None:
        """VM:MonitorViewModel 收到 `MessageEdited`(TDLib updateMessageContent)。

        按 (channel_id, telegram_msg_id) 找现有 row 替换 — 不增删。
        2026-08-24 上线,跟 LIVE 流 append 配合:append 跳过同 key,
        edit 走 replace_message 触发 dataChanged 信号 → 现有 cell 重渲。
        """
        self._ctx.live_view.replace_message(m)

    def on_message_interactions_changed(self, e: object) -> None:
        """VM:reactions / views 增量推送 → LIVE 行局部刷新 + 详情面板同步。

        2026-09-11 v1.7.4 上线。TDLib `updateMessageInteractionInfo` 服务
        端推送策略决定延迟;`updateMessageReactions` 是 bots-only,user
        client 不收 — 本路径靠 updateMessageInteractionInfo 拿到
        `reactions` / `views` 变化。Anonymous reactions 永远看不到。

        `e: object` 是 `MessageInteractionsChanged`,用 isinstance 兜底:
        EventBus 类型分发偶发接错 caller 的事件时直接 return,不污染状态。
        """
        if not isinstance(e, MessageInteractionsChanged):
            return
        # LIVE 行 reactions 局部刷新(reactions=None 时 refresh_reactions 内部忽略)
        self._ctx.live_view.refresh_reactions(e.channel_id, e.telegram_msg_id, e.reactions)
        # 详情面板同步:若正显示该消息,用 LIVE 模型里的最新 DTO 重建(reactions 已就地更新)
        dto = self._ctx.live_view.dto_by_key(e.channel_id, e.telegram_msg_id)
        if dto is not None:
            self._ctx.message_detail.refresh_if_showing(e.channel_id, e.telegram_msg_id)

    async def on_bus_message_deleted(self, e: object) -> None:
        """EventBus:MonitorService.delete_message 发的 MessageDeleted → LIVE 流删行。

        bytes 清理由 MonitorService.delete_message 内部已做,这里只管 UI。
        `async` 跟 EventBus subscriber 签名对齐(bus 对 sync / async 兼容),
        本 handler 当前不 await 任何东西,签名保留便于后续扩展(若想接
        async cache 清理).
        """
        if not isinstance(e, MessageDeleted):
            return
        self._ctx.live_view.remove_row(e.channel_id, e.telegram_msg_id)
        self._ctx.status_bar.show_activity(
            f"已删除 #{e.channel_id}/{e.telegram_msg_id}", timeout_ms=2000
        )
