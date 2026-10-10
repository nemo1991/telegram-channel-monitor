# mypy: disable-error-code="misc,assignment,override"
"""TDLib update 事件 handlers — `TdlibTelegramClient` 的 events 子系统。

2026-10-10 v1.12.x plan-batch2 目标 3 从 `TdlibTelegramClient` 抽出:
14 个 `_on_*` 事件 handler + 7 个 `_safe_publish_*` 事件发布 helper +
1 个 `_on_any_update` 全局 catch,合计 ~480 LOC。它们都不在 `TelegramClient`
Protocol 公开面,**纯内部 wiring**,走 composition delegate 后
`TdlibTelegramClient.__init__` 末尾的 12 个 `add_event_handler(...)` 全部
改走 `self._events.X` bound method。

# 设计理由

为什么不 mixin?
  - 同 `ChannelsApi`:events handler 用 `self._streams` / `self._bus` /
    `self._seen_error_codes` / `self._last_error_message` 等 lifecycle
    私有状态,纯 mixin 没这些
  - mixin 重写 `__init__` 会跟主类冲突,composition delegate 干净

# 公开 vs 私有

**全部私有** — Protocol 不暴露。caller 永远走 `TdlibTelegramClient` 公共
方法;events 子系统是内部 TDLib `updateXxx` → 业务事件的桥。

# 依赖方向

`tdlib_events.py` → `tdlib_messages._map_message`(已有独立模块)
`tdlib_events.py` → `tdlib_proxy._CONN_STATE_MAP`(已有独立模块)
`tdlib_events.py` → `tgmonitor.core.events.*`(业务事件类型)
`tdlib_events.py` 不反向 import `tdlib_client` — 无循环依赖。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from tgmonitor.core.telegram.tdlib_messages import _map_message
from tgmonitor.core.telegram.tdlib_proxy import _CONN_STATE_MAP

if TYPE_CHECKING:
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient

log = logging.getLogger(__name__)


class EventsHandler:
    """TDLib `updateXxx` → 业务事件(`EventBus`)的转换层。

    持 `client: TdlibTelegramClient` 引用以访问 lifecycle 资源
    (`_streams` / `_bus` / `_seen_error_codes` / `_last_error_message`)。

    不直接构造 — 由 `TdlibTelegramClient.__init__` 末尾建:
        self._events = EventsHandler(self)
    然后 `__init__` 用 `self._events._on_new_message` 等 bound method 注册:
        self.add_event_handler(self._events._on_new_message, update_type=...)
    """

    def __init__(self, client: TdlibTelegramClient) -> None:
        """`client` = 父 lifecycle 控制器(只持引用,不构造任何资源)。"""
        self._c = client

    # ============================================================
    # updateNewMessage / updateMessageContent  →  MessageDTO stream
    # ============================================================

    async def _on_new_message(self, client_self, update) -> None:
        """tdlib_json 用 `await handler(self, update)` 调用 handler,所以签名
        必须是 `(client, update)`,这里 `client_self` 其实是 client 实例本身
        (我们就是它),所以丢弃。"""
        try:
            msg = getattr(update, "message", None)
            if msg is None:
                log.debug(
                    "updateNewMessage received without message payload: %s",
                    getattr(update, "@type", type(update).__name__),
                )
                return
            log.debug(
                "updateNewMessage received: chat_id=%s msg_id=%s",
                getattr(msg, "chat_id", None),
                getattr(msg, "id", None),
            )
            dto = _map_message(msg)
            for s in list(self._c._streams):
                await s.push(dto)
        except Exception:  # noqa: BLE001
            log.exception("updateNewMessage handling failed")

    async def _on_message_edited(self, client_self, update) -> None:
        """updateMessageContent(2026-08-24):message 字段携带编辑后完整 Message 对象。

        走 `_map_message` 同样的入口,产出 DTO 后推给所有 `UpdateStream` —
        与 _on_new_message 共用 `_streams` 列表,DTO 类型不变。
        MonitorService 在 `_run` 用 `_seen_ids` 区分「新消息 vs 编辑」。

        不在 handler 侧做 whitelist 守门:编辑事件来自「已订阅时收到的消息历史」,
        用户可能后续退订了频道 — 编辑仍要落库,UI 显示该消息时按 channels_changed
        自行判断是否过滤。
        """
        try:
            msg = getattr(update, "message", None)
            if msg is None:
                log.debug(
                    "updateMessageContent received without message payload: %s",
                    getattr(update, "@type", type(update).__name__),
                )
                return
            log.debug(
                "updateMessageContent received: chat_id=%s msg_id=%s",
                getattr(msg, "chat_id", None),
                getattr(msg, "id", None),
            )
            dto = _map_message(msg)
            for s in list(self._c._streams):
                await s.push(dto)
        except Exception:  # noqa: BLE001
            log.exception("updateMessageContent handling failed")

    # ============================================================
    # updateConnectionState  →  ConnectionStateChanged 事件
    # ============================================================

    async def _on_connection_state(self, client_self, update) -> None:
        """TDLib 网络连接状态(updateConnectionState)→ `ConnectionStateChanged` 事件。

        状态来源是 TDLib 推送的 `connectionState*` 对象(如 connectionStateReady);
        这是 UI 底部状态栏"与 TG 的通信状态"的最准信号 — 代理 / DC 不通时它只会
        停在 waiting_for_network / connecting,一眼可见。
        """
        try:
            state_obj = getattr(update, "state", None)
            state_type = ""
            if isinstance(state_obj, dict):
                state_type = state_obj.get("@type", "")
            elif state_obj is not None:
                state_type = getattr(state_obj, "@type", "")
            new_state = _CONN_STATE_MAP.get(state_type, "unknown")
            log.info("tdlib connection state → %s (%s)", new_state, state_type)
            if self._c._bus is not None:
                # PR 1b:直接 create_task + 注册到 `bus._inflight` 给测试 `flush()` 等。
                task = asyncio.create_task(self._safe_publish_conn_state(new_state))
                self._c._bus._inflight.add(task)
                task.add_done_callback(self._c._bus._inflight.discard)
        except Exception:  # noqa: BLE001
            log.exception("connection state handling failed")

    # ============================================================
    # updateMessageInteractionInfo  →  MessageInteractionsChanged 事件
    # ============================================================

    async def _on_message_interactions(self, client_self, update) -> None:
        """TDLib `updateMessageInteractionInfo`(2026-08-27 v1.4.0 PR #10)
        — views 增量 + reactions 列表(可能空 list 表示被清空)。

        直接通过 bus 发 `MessageInteractionsChanged` 事件,MonitorService
        订阅并落库。**不**复用 `_streams`(那是 MessageDTO 流);高频
        增量走事件更轻 — 反正 storage 写入和 UI 刷新都要 bus 来通知。
        """
        try:
            chat_id = getattr(update, "chat_id", None)
            msg_id = getattr(update, "message_id", None)
            if chat_id is None or msg_id is None:
                log.debug(
                    "updateMessageInteractionInfo missing identifiers: %r",
                    getattr(update, "@type", type(update).__name__),
                )
                return
            from tgmonitor.core.telegram.tdlib_messages import _map_interaction_info

            new_views, new_reactions = _map_interaction_info(
                getattr(update, "interaction_info", None),
            )
            log.debug(
                "updateMessageInteractionInfo: chat=%s msg=%s views=%s reactions=%d",
                chat_id,
                msg_id,
                new_views,
                len(new_reactions),
            )
            if self._c._bus is not None:
                # PR 1b:直接 create_task + 注册到 `bus._inflight` 给测试 `flush()` 等。
                coro = self._safe_publish_interactions(
                    chat_id,
                    msg_id,
                    new_views,
                    new_reactions,
                )
                task = asyncio.create_task(coro)
                self._c._bus._inflight.add(task)
                task.add_done_callback(self._c._bus._inflight.discard)
        except Exception:  # noqa: BLE001
            log.exception("updateMessageInteractionInfo handling failed")

    # ============================================================
    # updateDeleteMessages  →  MessageDeleted 事件 (循环)
    # ============================================================

    async def _on_delete_messages(self, client_self, update) -> None:
        """TDLib `updateDeleteMessages`(2026-08-27 v1.4.0 PR #11)
        — TG 端撤回 / 删除消息推送;目前无 handler,落库消息永远存在,
        LIVE view / dashboard 计数被污染。

        通过 bus 发 `MessageDeleted`(循环发布,每条 msg_id 一条)。MonitorService
        订阅并落库删 row + 减 object_key refcount。
        """
        try:
            chat_id = getattr(update, "chat_id", None)
            msg_ids = getattr(update, "message_ids", None)
            if chat_id is None or not msg_ids:
                log.debug(
                    "updateDeleteMessages missing identifiers: %r",
                    getattr(update, "@type", type(update).__name__),
                )
                return
            log.info(
                "updateDeleteMessages: chat=%s count=%d",
                chat_id,
                len(msg_ids),
            )
            if self._c._bus is not None:
                # 每条 msg_id 单独 publish — UI 订阅端粒度更细。
                # PR 1b:直接 create_task + 注册到 `bus._inflight` 给测试 `flush()` 等。
                coro = self._safe_publish_bulk_delete(
                    int(chat_id),
                    [int(m) for m in msg_ids],
                )
                task = asyncio.create_task(coro)
                self._c._bus._inflight.add(task)
                task.add_done_callback(self._c._bus._inflight.discard)
        except Exception:  # noqa: BLE001
            log.exception("updateDeleteMessages handling failed")

    # ============================================================
    # updateMessageIsPinned  →  MessagePinChanged 事件
    # ============================================================

    # 2026-09-10 v1.7.3:TDLib `updateMessageIsPinned` 推送 — 服务端 pin /
    # unpin(任何端触发)同步到 UI。handler 注册见 `add_event_handler` 块。
    async def _on_message_pin_changed(self, client_self, update) -> None:
        """TDLib `updateMessageIsPinned`(2026-09-10 v1.7.3)— pin 状态变化。

        payload:`{chat_id, message_id, is_pinned}`(TDLib 把 message 包成
        `{chat_id, id}` 二元组;v1.7.2 之前无此 handler,server-side pin 永不
        推到 UI,LIVE 行 📌 图标只随本地 `AppService.pin_messages` RPC 后
        自己改 DTO,从未响应外部变化)。

        通过 bus 发 `MessagePinChanged`,MonitorService 订阅并落库 + re-fetch
        DTO + publish `MessageEdited` 触发 UI 行 📌 刷新。
        """
        try:
            chat_id = getattr(update, "chat_id", None)
            msg_id = getattr(update, "message_id", None)
            is_pinned = getattr(update, "is_pinned", None)
            if chat_id is None or msg_id is None or is_pinned is None:
                log.debug(
                    "updateMessageIsPinned missing identifiers: %r",
                    getattr(update, "@type", type(update).__name__),
                )
                return
            log.info(
                "updateMessageIsPinned: chat=%s msg=%s pinned=%s",
                chat_id,
                msg_id,
                is_pinned,
            )
            if self._c._bus is not None:
                # PR 1b:直接 create_task + 注册到 `bus._inflight` 给测试 `flush()` 等。
                coro = self._safe_publish_pin_changed(
                    int(chat_id),
                    int(msg_id),
                    bool(is_pinned),
                )
                task = asyncio.create_task(coro)
                self._c._bus._inflight.add(task)
                task.add_done_callback(self._c._bus._inflight.discard)
        except Exception:  # noqa: BLE001
            log.exception("updateMessageIsPinned handling failed")

    # ============================================================
    # updateChannel / updateSupergroup  →  ChannelMetadataChanged 事件
    # ============================================================

    async def _on_channel_updated(self, client_self, update) -> None:
        """TDLib `updateChannel`(2026-08-27 v1.4.0 PR #14):频道元数据
        增量更新(title / username)。TDLib 推 channel chat 完整对象,
        `chat.id` = `channel_id`(Telegram chat_id)。

        直接通过 bus 发 `ChannelMetadataChanged`,MonitorService 订阅
        落库 + UI 实时刷新。

        2026-09-04 v1.6.4:同时透传 `chat.has_protected_content`(Chat 顶层
        字段,channel / private 都有;basic_group 没,固定 False)。
        """
        try:
            channel = getattr(update, "channel", None)
            if channel is None:
                return
            cid = getattr(channel, "id", None)
            if cid is None:
                return
            new_title = getattr(channel, "title", None)
            new_username = getattr(channel, "username", None)
            # 2026-09-04 v1.6.4:`has_protected_content` 是 Chat 顶层字段 —
            # 该字段在 `Channel` 类型(公开频道 / 私有 chat)上始终存在;
            # basic_group 没该字段,但 `updateChannel` 不可能推送 basic_group。
            new_has_protected = bool(getattr(channel, "has_protected_content", False))
            log.debug(
                "updateChannel: id=%s title=%r username=%r has_protected=%s",
                cid,
                new_title,
                new_username,
                new_has_protected,
            )
            if self._c._bus is not None:
                # PR 1b:直接 create_task + 注册到 `bus._inflight` 给测试 `flush()` 等。
                coro = self._safe_publish_channel_metadata(
                    channel_id=int(cid),
                    title=new_title,
                    username=new_username,
                    member_count=None,
                    has_protected_content=new_has_protected,
                )
                task = asyncio.create_task(coro)
                self._c._bus._inflight.add(task)
                task.add_done_callback(self._c._bus._inflight.discard)
        except Exception:  # noqa: BLE001
            log.exception("updateChannel handling failed")

    async def _on_supergroup_updated(self, client_self, update) -> None:
        """TDLib `updateSupergroup`(2026-08-27 v1.4.0 PR #14):supergroup
        元数据增量更新(member_count 改、username 改等)。

        注意:`updateSupergroup` 只携带 supergroup_id,**不**含 channel_id —
        monitor 端要做 supergroup_id ↔ channel_id 映射落库。这一层只在
        bus 上 publish `(supergroup_id=, member_count=, username=)`;具体
        落到哪个 channel_id 由 MonitorService 决定。

        2026-09-04 v1.6.4:同时透传 `is_verified` / `is_scam` / `is_fake`
        (Supergroup 顶层字段,spammer 过滤 UI 用)。
        """
        try:
            supergroup = getattr(update, "supergroup", None)
            if supergroup is None:
                return
            sid = getattr(supergroup, "id", None)
            if sid is None:
                return
            member_count = getattr(supergroup, "member_count", None)
            username = getattr(supergroup, "username", None)
            # 2026-09-04 v1.6.4:`is_verified`/`is_scam`/`is_fake` 是
            # Supergroup 顶层字段(基本不会变,但 TDLib 会随 verified
            # 勾选 toggle 推送)。
            sg_is_verified = bool(getattr(supergroup, "is_verified", False))
            sg_is_scam = bool(getattr(supergroup, "is_scam", False))
            sg_is_fake = bool(getattr(supergroup, "is_fake", False))
            log.debug(
                "updateSupergroup: id=%s member_count=%s username=%r verified=%s scam=%s fake=%s",
                sid,
                member_count,
                username,
                sg_is_verified,
                sg_is_scam,
                sg_is_fake,
            )
            if self._c._bus is not None:
                # PR 1b:直接 create_task + 注册到 `bus._inflight` 给测试 `flush()` 等。
                coro = self._safe_publish_supergroup_metadata(
                    supergroup_id=int(sid),
                    member_count=member_count,
                    username=username,
                    is_verified=sg_is_verified,
                    is_scam=sg_is_scam,
                    is_fake=sg_is_fake,
                )
                task = asyncio.create_task(coro)
                self._c._bus._inflight.add(task)
                task.add_done_callback(self._c._bus._inflight.discard)
        except Exception:  # noqa: BLE001
            log.exception("updateSupergroup handling failed")

    # ============================================================
    # 6 类普通 chat update handler(2026-09-03 v1.6.0 PR #Q2)
    # ============================================================

    # ---- 2026-09-03 v1.6.0 PR #Q2:补 6 类 TDLib chat update handler ----
    # `updateChannel` / `updateSupergroup` 只覆盖 channel / supergroup 元数据;
    # 普通频道(chat)改名 / 改头像 / 改权限 / 读位置 / 默认封禁权 由独立
    # update 推送。本段 6 个 handler 全部仅「log + publish 事件」,落库走
    # MonitorService 订阅事件统一处理(避免 handler 与 service 双向耦合)。

    async def _on_chat_title(self, client_self, update) -> None:
        """TDLib `updateChatTitle`(2026-09-03 v1.6.0 PR #Q2):普通频道标题变化。

        update 结构:`{chat_id: int, title: str}`。chat_id 即 channel_id
        (Telegram 全局 chat_id,与 channel 一致)。改名后 UI 实时刷 +
        落库走 ChannelTitleChanged 事件订阅方。
        """
        try:
            chat_id = getattr(update, "chat_id", None)
            title = getattr(update, "title", None)
            if not isinstance(chat_id, int) or not isinstance(title, str):
                return
            log.debug("updateChatTitle: chat_id=%s title=%r", chat_id, title)
            if self._c._bus is not None:
                from tgmonitor.core.events import ChannelTitleChanged

                # PR 1b:`publish_async` 注册 task 到 `_inflight`,测试可 `await bus.flush()`
                # 等投递完成 — 替代 `await asyncio.sleep(0.05)`。
                self._c._bus.publish_async(ChannelTitleChanged(channel_id=chat_id, new_title=title))
        except Exception:  # noqa: BLE001
            log.exception("updateChatTitle handling failed chat_id=%s", chat_id)

    async def _on_chat_photo(self, client_self, update) -> None:
        """TDLib `updateChatPhoto`(2026-09-03 v1.6.0 PR #Q2):频道头像变化。

        update 结构:`{chat_id: int, photo: {local: {path: str}} | None}`。
        `photo=None` 表示头像被删;`photo.local.path` 是 TDLib 本地缓存
        绝对路径(frozen / sandbox 不可访问时 UI 走 placeholder)。
        """
        try:
            chat_id = getattr(update, "chat_id", None)
            if not isinstance(chat_id, int):
                return
            photo = getattr(update, "photo", None)
            local_path: str | None = None
            if isinstance(photo, dict):
                local = photo.get("local")
                if isinstance(local, dict):
                    path = local.get("path")
                    if isinstance(path, str):
                        local_path = path
            log.debug("updateChatPhoto: chat_id=%s local_path=%r", chat_id, local_path)
            if self._c._bus is not None:
                from tgmonitor.core.events import ChannelPhotoChanged

                # PR 1b:`publish_async` — 测试可用 `bus.flush()` 等投递完成。
                self._c._bus.publish_async(
                    ChannelPhotoChanged(channel_id=chat_id, local_path=local_path)
                )
        except Exception:  # noqa: BLE001
            log.exception("updateChatPhoto handling failed chat_id=%s", chat_id)

    async def _on_chat_permissions(self, client_self, update) -> None:
        """TDLib `updateChatPermissions`(2026-09-03 v1.6.0 PR #Q2):频道成员权限变化。

        目前仅 log — 暂无 storage 字段映射(后续如需 ban / mute 元数据可加)。
        保留 handler 防未知 update 静默丢失。

        2026-09-04 v1.6.4:不携带 4 类 spammer 字段(`is_verified` /
        `is_scam` / `is_fake` / `has_protected_content`),无 storage 落库
        路径。
        """
        try:
            chat_id = getattr(update, "chat_id", None)
            permissions = getattr(update, "permissions", None)
            log.info("updateChatPermissions: chat_id=%s perms=%r", chat_id, permissions)
        except Exception:  # noqa: BLE001
            log.exception("updateChatPermissions handling failed")

    async def _on_chat_read_inbox(self, client_self, update) -> None:
        """TDLib `updateChatReadInbox`(2026-09-03 v1.6.0 PR #Q2):已读位置变化。

        目前仅 log — 暂无 storage 字段映射。

        2026-09-04 v1.6.4:不携带 spammer 字段。
        """
        try:
            chat_id = getattr(update, "chat_id", None)
            last_read = getattr(update, "last_read_inbox_message_id", None)
            log.info(
                "updateChatReadInbox: chat_id=%s last_read_msg_id=%s",
                chat_id,
                last_read,
            )
        except Exception:  # noqa: BLE001
            log.exception("updateChatReadInbox handling failed")

    async def _on_chat_read_outbox(self, client_self, update) -> None:
        """TDLib `updateChatReadOutbox`(2026-09-03 v1.6.0 PR #Q2):已发送消息被读位置。

        目前仅 log。

        2026-09-04 v1.6.4:不携带 spammer 字段。
        """
        try:
            chat_id = getattr(update, "chat_id", None)
            last_read = getattr(update, "last_read_outbox_message_id", None)
            log.info(
                "updateChatReadOutbox: chat_id=%s last_read_msg_id=%s",
                chat_id,
                last_read,
            )
        except Exception:  # noqa: BLE001
            log.exception("updateChatReadOutbox handling failed")

    async def _on_chat_default_banned_rights(self, client_self, update) -> None:
        """TDLib `updateChatDefaultBannedRights`(2026-09-03 v1.6.0 PR #Q2):
        频道「默认封禁权」变化(新成员加入时的初始权限)。

        目前仅 log。

        2026-09-04 v1.6.4:不携带 spammer 字段(`has_protected_content` 是
        Chat 顶层字段,与 default_banned_rights 语义无关)。
        """
        try:
            chat_id = getattr(update, "chat_id", None)
            rights = getattr(update, "default_banned_rights", None)
            log.info("updateChatDefaultBannedRights: chat_id=%s rights=%r", chat_id, rights)
        except Exception:  # noqa: BLE001
            log.exception("updateChatDefaultBannedRights handling failed")

    # ============================================================
    # 全局 catch — Error 包 → 错误码收集
    # ============================================================

    async def _on_any_update(self, client_self, update) -> None:
        """全局 catch handler — 任何 update 进来都看一眼,把 Error 包的 code 记录下来。

        tdlib_json 用 `await handler(self, update)` 调用,所以必须 (client, update)。
        原版是 `__init__` 内嵌闭包(需要写 `self._c._seen_error_codes` /
        `self._c._last_error_message`),抽到 EventsHandler 后改为普通方法 —
        closure 跟 method 行为等价(都写 self._c.*),方法形式可被 mypy 检查。
        """
        try:
            if update.get("@type") == "error":
                code = update.get("code")
                if isinstance(code, int):
                    self._c._seen_error_codes.append(code)
                    msg = str(update.get("message", ""))
                    self._c._last_error_message = msg
                    log.warning("tdlib_json Error observed: code=%s msg=%s", code, msg)
        except Exception:  # noqa: BLE001
            log.exception("_on_any_update handling failed (suppressed)")

    # ============================================================
    # `_safe_publish_*` helpers — EventBus 投递,异常被吞
    # ============================================================

    async def _safe_publish_interactions(
        self,
        chat_id: int,
        msg_id: int,
        views: int | None,
        reactions: list,
    ) -> None:
        try:
            from tgmonitor.core.events import MessageInteractionsChanged

            assert self._c._bus is not None
            await self._c._bus.publish(
                MessageInteractionsChanged(
                    channel_id=int(chat_id),
                    telegram_msg_id=int(msg_id),
                    views=views,
                    reactions=reactions,
                )
            )
        except Exception:  # noqa: BLE001
            log.exception("publish MessageInteractionsChanged failed")

    async def _safe_publish_bulk_delete(
        self,
        chat_id: int,
        msg_ids: list[int],
    ) -> None:
        try:
            from tgmonitor.core.events import MessageDeleted

            assert self._c._bus is not None
            for mid in msg_ids:
                await self._c._bus.publish(
                    MessageDeleted(
                        channel_id=chat_id,
                        telegram_msg_id=mid,
                    )
                )
        except Exception:  # noqa: BLE001
            log.exception("publish bulk MessageDeleted failed")

    async def _safe_publish_pin_changed(
        self,
        chat_id: int,
        msg_id: int,
        is_pinned: bool,
    ) -> None:
        try:
            from tgmonitor.core.events import MessagePinChanged

            assert self._c._bus is not None
            await self._c._bus.publish(
                MessagePinChanged(
                    channel_id=chat_id,
                    telegram_msg_id=msg_id,
                    is_pinned=is_pinned,
                )
            )
        except Exception:  # noqa: BLE001
            log.exception("publish MessagePinChanged failed")

    async def _safe_publish_channel_metadata(
        self,
        channel_id: int,
        title: str | None,
        username: str | None,
        member_count: int | None,
        has_protected_content: bool | None = None,  # 2026-09-04 v1.6.4
    ) -> None:
        try:
            from tgmonitor.core.events import ChannelMetadataChanged

            assert self._c._bus is not None
            await self._c._bus.publish(
                ChannelMetadataChanged(
                    channel_id=channel_id,
                    title=title,
                    username=username,
                    member_count=member_count,
                    has_protected_content=has_protected_content,
                )
            )
        except Exception:  # noqa: BLE001
            log.exception("publish ChannelMetadataChanged failed")

    async def _safe_publish_supergroup_metadata(
        self,
        supergroup_id: int,
        member_count: int | None,
        username: str | None,
        is_verified: bool | None = None,  # 2026-09-04 v1.6.4
        is_scam: bool | None = None,
        is_fake: bool | None = None,
    ) -> None:
        try:
            from tgmonitor.core.events import ChannelMetadataChanged

            assert self._c._bus is not None
            await self._c._bus.publish(
                ChannelMetadataChanged(
                    channel_id=0,  # 0 = monitor 通过 supergroup_id 解析
                    supergroup_id=supergroup_id,
                    title=None,
                    username=username,
                    member_count=member_count,
                    is_verified=is_verified,
                    is_scam=is_scam,
                    is_fake=is_fake,
                )
            )
        except Exception:  # noqa: BLE001
            log.exception("publish supergroup ChannelMetadataChanged failed")

    async def _safe_publish_conn_state(self, state: str) -> None:
        try:
            from tgmonitor.core.events import ConnectionStateChanged

            assert self._c._bus is not None
            await self._c._bus.publish(ConnectionStateChanged(state=state))
        except Exception:  # noqa: BLE001
            log.exception("publish ConnectionStateChanged failed")
