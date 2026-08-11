"""Channels API — Telegram Data API 操作 composition 类。

模块拆分(2026-08-02):从 `tdlib_client.py` 抽出 6 个 channels 方法 +
2 个内部 helper 到本文件,以 `ChannelsApi` 形式持 `TdlibTelegramClient`
引用。lifecycle controller 上同名方法改为 thin delegate。

# 设计理由

为什么不 mixin / 多继承?
  - channels 方法用 lifecycle 私有状态(`_check_alive` / `_state` /
    `_closing` / `_wait_for_state`),纯 mixin 没这些
  - 多继承让 ChannelsApi 跟 TdlibTelegramClient 同 MRO,等于不拆

为什么不单挑出几个 pure function?
  - 6 个方法都直接 `self.request(...)` / `self.send(...)` tdlib_json 桥
  - 把 `request` 当参数传进去也行,但调用点 6×N 处改起来不值

# 公开 vs 私有

公开(由 Protocol 暴露):`get_channel_metadata` / `list_joined_channels` /
`iter_chat_history` / `join_channel` / `download_file`
私有:`_resolve_channel_metadata` / `_iter_resolved_chats`

# 依赖方向

`tdlib_channels.py` → `tdlib_messages._map_message`(已有独立模块)
`tdlib_channels.py` → `tdlib_errors.ClientClosingError`(已有独立模块)
`tdlib_channels.py` 不反向 import `tdlib_client` — 无循环依赖。
"""
# mypy: disable-error-code="misc,assignment"
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, AsyncIterator

from tgmonitor.core.dto import ChannelDTO, MessageDTO
from tgmonitor.core.telegram.tdlib_errors import ClientClosingError
from tgmonitor.core.telegram.tdlib_messages import _map_message

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient


class ChannelsApi:
    """Telegram Data API — channels 子系统的 composition 包装。

    持 `client: TdlibTelegramClient` 引用以访问 lifecycle 资源
    (`request()` / `send()` / `_check_alive()` / `_state` / `_closing` /
    `_wait_for_state(...)`)。

    不直接构造 — 由 `TdlibTelegramClient.__init__` 末尾建:
        self.channels = ChannelsApi(self)
    """

    def __init__(self, client: TdlibTelegramClient) -> None:
        """`client` = 父 lifecycle 控制器(只持引用,不构造任何资源)。"""
        self._c = client

    # `list_joined_channels` 非 ready 时等 ready 的最大秒数(best-effort);
    # 独立类属性让测试可调小,免得每个 state 分支都干等 8s。
    _READY_WAIT_TIMEOUT = 8.0

    # ============================================================
    # 元数据
    # ============================================================

    async def _resolve_channel_metadata(self, chat_id: int) -> ChannelDTO | None:
        """GetChat + GetSupergroup/GetBasicGroup 拿完整元数据。

        修 `tdlib_client.py:818-819` 旧 bug:`getattr(chat, "username", None)`
        永远拿不到 — `Chat` 类型没 username / member_count,这些在
        `Supergroup` / `BasicGroup` 上。
        """
        chat = await self._c.request({"@type": "getChat", "chat_id": chat_id})
        if chat is None:
            return None
        ct = getattr(chat, "type_", None) or getattr(chat, "type", None)
        title = chat.title
        if ct is not None and ct.get("@type") == "chatTypeSupergroup":
            is_channel = bool(getattr(ct, "is_channel", False))
            kind = "channel" if is_channel else "supergroup"
            sg = await self._c.request(
                {"@type": "getSupergroup", "supergroup_id": ct.supergroup_id}
            )
            username = None
            member_count = None
            if sg is not None:
                usernames = getattr(sg, "usernames", None)
                if usernames is not None:
                    active = getattr(usernames, "active_usernames", None) or []
                    if active:
                        username = active[0]
                mc = getattr(sg, "member_count", None)
                if isinstance(mc, int) and mc > 0:
                    member_count = mc
            return ChannelDTO(
                id=chat_id, title=title, username=username, kind=kind,
                member_count=member_count,
            )
        if ct is not None and ct.get("@type") == "chatTypeBasicGroup":
            bg = await self._c.request(
                {"@type": "getBasicGroup", "basic_group_id": ct.basic_group_id}
            )
            member_count = None
            if bg is not None:
                mc = getattr(bg, "member_count", None)
                if isinstance(mc, int) and mc > 0:
                    member_count = mc
            return ChannelDTO(
                id=chat_id, title=title, username=None, kind="basic_group",
                member_count=member_count,
            )
        return None  # private / secret — 同步功能不覆盖

    async def get_channel_metadata(self, channel_id: int) -> ChannelDTO:
        """ChannelSyncService 用:拉一个频道的最新元数据。"""
        self._c._check_alive()
        dto = await self._resolve_channel_metadata(channel_id)
        if dto is None:
            # 私有/secret 或 chat 不存在,fallback 给个 stub
            return ChannelDTO(id=channel_id, title=f"#{channel_id}")
        return dto

    async def list_joined_channels(self) -> list[ChannelDTO]:
        """列已加入频道 — best-effort UX,详见下方注释。

        # best-effort UX:被 VM `_go` 在三种时机 fire-and-forget 调用:
        #   1) close() 中途
        #   2) startup 时 bridge 还没 ready(VM 的 `bootstrap_ui` 在
        #      `app.bootstrap()` 完成前后 fire 了 `list_*`,
        #      但 bridge/_state="ready" 还没等到 — 真打开 app 时撞这个)
        #   3) LoginStateChanged 转 ready 后 VM 再拉一次
        # 这三种情况都"安静走",不撞 tdlib_json 10s request_timeout,
        # 让 VM 自然 idle,等下次 LoginStateChanged 或用户点 Refresh 再触发。
        #
        # 关键(2026-07-18 修复):之前 `if self._state != "ready": return []`
        # 立即返回,但**bootstrap race 路径下**老版本会错过稍后才到的 "ready":
        #   - `start()` 等的是 `_state_event.wait()`,任何状态变化都 set,
        #     所以 tdlib_json 触发 `updateAuthorizationState(WaitTdlibParameters)`
        #     就可能让 start() 提前返(state="tdlib_parameters")
        #   - VM.bootstrap_ui 紧接着 fire list_joined_channels
        #   - guard 看到 state != "ready" → 立即 [],错过 200ms 后到的 "ready"
        #   - channels 永不显示,直到用户手动 Refresh
        # 现在改成"非 ready 时短暂等待再判"。
        """
        if self._c._closing:
            log.info("[tdlib] list_joined_channels: client closing, returning []")
            return []
        if self._c._state != "ready":
            # 等 ≤ N 秒让 tdlib_json 完成从 Wait* → Ready 的过渡
            # 仍 best-effort:超过 N 秒还没 ready(网络挂了/401/...)就 []
            try:
                await self._c._wait_for_state(
                    "ready", timeout=self._READY_WAIT_TIMEOUT
                )
            except TimeoutError:
                log.debug(
                    "[tdlib] list_joined_channels: state=%r "
                    "(未到 ready,%.1fs 超时)",
                    self._c._state, self._READY_WAIT_TIMEOUT,
                )
                return []
            if self._c._state != "ready":
                return []
        import time as _t
        t0 = _t.monotonic()
        result: list[ChannelDTO] = []
        try:
            t = _t.monotonic()
            chats = await self._c.request(
                {"@type": "getChats", "chat_list": {"@type": "chatListMain"}, "limit": 200}
            )
            log.info("[tdlib] GetChats(limit=200) returned %d ids in %.3fs",
                     len(chats.chat_ids) if chats and chats.chat_ids else 0,
                     _t.monotonic() - t)
            if chats is None:
                return result
            async for dto in self._iter_resolved_chats(
                chats.chat_ids or [], t0,
            ):
                result.append(dto)
        except ClientClosingError:
            # mid-loop 命中 `_check_alive()` —— 用户关窗 / 重启触发了 close(),
            # 静默退出,不再打 traceback
            log.info("[tdlib] list_joined_channels: aborted (client closing)")
        except Exception:  # noqa: BLE001
            log.exception("list_joined_channels failed")
        log.info("[tdlib] list_joined_channels done: %d channels in %.2fs",
                 len(result), _t.monotonic() - t0)
        return result

    async def _iter_resolved_chats(
        self,
        chat_ids: list[int],
        t0: float,
    ) -> AsyncIterator[ChannelDTO]:
        """把 GetChats 拿到的 chat_id 列表逐个解析成 ChannelDTO。

        抽出来是为了:
          1. `list_joined_channels` 只剩 lifecycle guard + GetChats + 聚合,
             单方法 30 行以里,可读;
          2. 单条解析失败 / mid-loop close 是迭代器的事(每个 yield 一个 DTO),
             caller 专心 aggregate。

        边界:
          - `_check_alive()` 中途命中 → 抛 ClientClosingError(让 caller 静默 catch);
          - 单条 `_resolve_channel_metadata` 失败 → log + skip(不影响其他 cid);
          - `_resolve_channel_metadata` 返 None(private / secret chat)→ skip;
          - n_total >= 50 时每 50 条打一次 progress(debug 友好)。
        """
        import time as _t

        n_total = len(chat_ids)
        for i, cid in enumerate(chat_ids):
            # 每个 cid 解析前再 check 一次 —— 拉 mid-loop 时已经被 close()
            # 也不要把这条请求继续排进 tdlib_json bridge
            self._c._check_alive()
            try:
                dto = await self._resolve_channel_metadata(cid)
            except ClientClosingError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("_resolve_channel_metadata(%d) failed", cid)
                continue
            if dto is None:
                continue
            if n_total >= 50 and (i + 1) % 50 == 0:
                log.info("[tdlib] list_joined_channels progress %d/%d in %.2fs",
                         i + 1, n_total, _t.monotonic() - t0)
            yield dto

    # ============================================================
    # 历史消息分页(全量同步用)
    # ============================================================

    async def iter_chat_history(
        self,
        channel_id: int,
        *,
        before_msg_id: int = 0,
        limit: int = 100,
    ) -> AsyncIterator[MessageDTO]:
        """分页拉取频道历史消息(向旧方向递减)。

        before_msg_id=0 → 拉最新 N 条;>0 → 从该 id 之前(更早)开始续拉。
        TDLib `getChatHistory.from_message_id` 只支持向旧方向翻页,所以传
        参语义是"截止这条之前",而非"从这条之后"。翻页游标 = 本批最小 id。
        限流:每页间不 sleep(由调用方 ChannelSyncService 控)。
        """
        # Async generator:`_check_alive()` 在每次分页入口 throw,中途 close() 就
        # 立刻结束迭代(不再排下一页 getChatHistory request,免得撞 10s 超时 +
        # 跨 loop wakeup 噪音)
        while True:
            self._c._check_alive()
            resp = await self._c.request({
                "@type": "getChatHistory",
                "chat_id": channel_id,
                "from_message_id": before_msg_id,
                "offset": 0,
                "limit": limit,
            })
            if resp is None or not getattr(resp, "messages", None):
                break
            batch = list(resp.messages)
            for raw in batch:
                if raw is None:
                    continue
                # _map_message 自己从 msg.chat_id 取 channel_id,
                # 不需要外面传;这里只 yield
                yield _map_message(raw)
            # TDLib 文档:limit<=100;返回数 < limit → 已到尽头
            if len(batch) < limit:
                break
            # 续拉:用本批最末(最小)id 作为下次 from_message_id
            last_id = None
            for raw in batch:
                rid = getattr(raw, "id", None)
                if rid is not None and (last_id is None or rid < last_id):
                    last_id = rid
            if last_id is None or last_id == before_msg_id:
                break
            before_msg_id = last_id

    async def join_channel(self, identifier: str) -> ChannelDTO:
        """SearchPublicChat + JoinChat。"""
        self._c._check_alive()
        username = identifier.lstrip("@") if identifier.startswith("@") else identifier
        # search 要拿响应 → request;join 不需要响应 → send
        resp = await self._c.request({"@type": "searchPublicChat", "username": username})
        if resp is None:
            raise RuntimeError(f"SearchPublicChat 返回空: {username!r}")
        await self._c.send({"@type": "joinChat", "chat_id": resp.id})
        return ChannelDTO(id=resp.id, title=resp.title, username=resp.username or None)

    # ============================================================
    # 媒体下载(REVIEW M2.1 — 真实现)
    # ============================================================

    async def download_file(self, file_id: str) -> bytes | None:
        """两步下载原文件 bytes;失败 / 超时返 None,**不抛**(让 monitor 循环继续)。

        步骤:
          1) downloadFile(synchronous=False) 触发后台下载(priority=1, 不等)。
          2) getFile 轮询直到 `local.is_downloading_completed`;读 `local.path`。
          3) 边界:
             - 入口 _check_alive():close 中 throw ClientClosingError(已有)。
             - 30 min hard cap:超过 → 返 None + WARNING。
             - getFile 返 None / path 缺失 → 返 None + WARNING。
        """
        import asyncio as _aio
        import time as _t
        from pathlib import Path as _Path

        self._c._check_alive()
        # 1) 触发后台下载(不等 — downloadFile synchronous=False)
        try:
            await self._c.request({
                "@type": "downloadFile",
                "file_id": file_id,
                "priority": 1,
                "synchronous": False,
            })
        except ClientClosingError:
            raise  # 让 close() 路径正常 throw,monitor loop 兜底
        except Exception as e:  # noqa: BLE001
            log.warning("DownloadFile(%s) failed: %s", file_id, e)
            return None

        # 2) 轮询直到 complete 或 hard cap
        deadline = _t.monotonic() + 1800.0  # 30 min
        while _t.monotonic() < deadline:
            self._c._check_alive()
            try:
                f = await self._c.request({"@type": "getFile", "file_id": file_id})
            except ClientClosingError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("GetFile(%s) failed: %s", file_id, e)
                return None
            if f is None:
                log.warning("GetFile(%s) returned None", file_id)
                return None
            local = getattr(f, "local", None)
            if local is None:
                log.warning("GetFile(%s).local is None", file_id)
                return None
            if getattr(local, "is_downloading_completed", False):
                path = getattr(local, "path", None)
                if not path:
                    log.warning("GetFile(%s).local.path missing on complete", file_id)
                    return None
                try:
                    # Path.read_bytes 是 sync IO;asyncio.to_thread 把
                    # 它 off-loop 跑,免得在 qasync / uvloop loop 上 block。
                    return await _aio.to_thread(_Path(path).read_bytes)
                except OSError as e:
                    log.warning("read_bytes(%s) failed: %s", path, e)
                    return None
            await _aio.sleep(0.5)

        log.warning("download_file(%s) timed out after 30 min", file_id)
        return None
