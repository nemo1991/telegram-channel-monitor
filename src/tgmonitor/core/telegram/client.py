"""TelegramClient — 业务侧接口(Protocol)。

`core/telegram` 子包把 Telegram 后端封装在这里,
其他模块只见这层接口,不见后端类型。

实现见 `telethon_client.py`;UI / 测试用 `FakeTelegramClient`。
"""

from __future__ import annotations

from typing import AsyncIterator, Awaitable, Callable, Protocol, runtime_checkable

from tgmonitor.core.dto import ChannelDTO, MessageDTO


@runtime_checkable
class TelegramClient(Protocol):
    """业务侧唯一的 Telegram 客户端接口。"""

    # ---- 鉴权 ----
    async def start(self) -> tuple[str, str | None]:
        """应用启动入口。返回 (state, detail)。state ∈ {ready, phone_required, error, ...}。"""
        ...

    async def submit_phone(self, phone: str) -> tuple[str, str | None]:
        """提交手机号 — 进入 `code_required`。返回 (state, detail)。"""
        ...

    async def submit_code(self, code: str) -> tuple[str, str | None]:
        """提交验证码。返回 (state, detail)。错误时**不**改顶层状态,
        改通过 `AuthErrorOccurred` 事件通知 UI。"""
        ...

    async def submit_password(self, password: str) -> tuple[str, str | None]:
        """提交 2FA 密码。返回 (state, detail)。"""
        ...

    async def submit_email(self, email: str) -> tuple[str, str | None]:
        """`email_required` 时提交邮箱地址。推进到 `email_code_required`
        (已有账号)或 `registration_required`(新账号)。"""
        ...

    async def submit_email_code(self, code: str) -> tuple[str, str | None]:
        """`email_code_required` 时提交邮箱验证码 → 通常推到 `ready`。"""
        ...

    async def submit_registration(
        self, first_name: str, last_name: str = ""
    ) -> tuple[str, str | None]:
        """`registration_required` 时注册新账号;成功 → `ready`。"""
        ...

    async def logout(self) -> None:
        """登出 — 清掉 session;caller 重建 client。"""
        ...

    async def stop(self) -> None:
        """2026-09-03 v1.6.1:暂停监听用 — 断开连接但保留 session 文件,
        后续 `start()` 可秒过(已 login)或走 phone/code/password(冷启动)。

        与 `close()` 区别:
        - `stop()` 保留 session(下次 start 自动 ready);`close()` 杀后台 task
        - `stop()` 后可 `start()` 回到 ready;`close()` 后再 start 等同冷启动
        """
        ...

    async def close(self) -> None:
        """关停后台 task — app exit 时必调,否则更新流 / watchdog 吊着 loop 不放。"""
        ...

    @property
    def state(self) -> str:
        """当前顶层状态(`uninit` / `phone_required` / `code_required` /
        `password_required` / `ready` / `error` / `closing`)。
        """
        ...

    @property
    def me(self) -> dict | None:
        """当前登录用户 {id, username, first_name, ...};未登录时 None。"""
        ...

    # ---- 频道 ----
    async def list_joined_channels(self) -> list[ChannelDTO]:
        """已加入频道列表(best-effort UX:close / 未 ready 时返 [],不抛)。"""
        ...

    async def join_channel(self, identifier: str) -> ChannelDTO:
        """identifier: @username 或 t.me/... 链接。"""
        ...

    async def get_channel_metadata(self, channel_id: int) -> ChannelDTO:
        """拉取频道的最新元数据(title/username/member_count/kind)。

        走 get_entity + GetFullChannel — 修原 list_joined_channels 元数据
        bug:username / member_count 不在 dialog 摘要上,要拉 full chat。
        ChannelSyncService 用这个拉元数据。
        """
        ...

    # ---- 消息流 ----
    def iter_chat_history(
        self, channel_id: int, *, before_msg_id: int = 0, limit: int = 100
    ) -> AsyncIterator[MessageDTO]:
        """分页拉取频道历史消息(ChannelSyncService 续拉用)。

        **方向约束:只能向旧方向拉**(id 递减)。即使想"从某条之后正向拉
        更新",`iter_chat_history` 也**不**支持 — 想拿最新消息请调
        `subscribe_updates()` 的实时流。

        参数:
          - `before_msg_id=0`:拉该频道最新 N 条(反向翻页直到耗尽)
          - `before_msg_id>0`:从 `before_msg_id` 之前(更早的)消息开始拉
            —— 续拉场景把已知的最小 id 传进来,实现会把本批**最后**一条
            (id 最小)的 id 作为下次入参,直到消息耗尽(返回 <limit 条时结束)
        """
        ...

    # ---- 媒体下载 ----
    async def download_file(
        self,
        file_id: str,
        *,
        progress_callback: Callable[[int, int | None], Awaitable[None]] | None = None,
    ) -> bytes | None:
        """下载媒体文件原 bytes;失败 / 超时返 None,**不抛**。

        实现约定(给 MediaDownloader 用的契约):
          - `file_id` 是消息定位符(`tl:{chat_id}:{msg_id}[:thumb]`),实现
            重取消息后下载(自动刷新过期的文件引用)。
          - 失败(网络 / 权限 / 30 min hard cap)→ 返 None,monitor 循环继续。
          - 真拿到 bytes 才返非 None bytes。

        `progress_callback`(2026-09-01 v1.5.1 PR #B3):可选 `await
        callback(downloaded: int, total: int | None)`;实现约 0.5s 节流
        一次,完成时发终值。`total=None` 表示 file_size 未知(只显示已
        下载字节)。默认 None = 不上报(老调用方零改动)。
        """
        ...

    def subscribe_updates(self) -> UpdateStream:
        """实时更新订阅,返回 AsyncIterator 形式,生命周期内持续 yield 消息 DTO。

        **契约**:caller 必须在订阅终止时调 `stream.aclose()`(或 `aclose()` 隐式
        触发)。未调用会:
          - Implementation 侧:stream 持续占位 `_streams`,长会话这列表只增不减
          - PySider6 侧:async generator 退出但 queue 未被 push `None`,`__anext__`
            阻塞
        """
        ...

    async def mark_messages_read(self, channel_id: int, msg_ids: list[int]) -> None:
        """2026-09-08 v1.7.0:批量标已读。

        `msg_ids` 长度无硬限制;实现内部 batch。fire-and-forget,
        抛错由调用方捕获(典型:client paused / 网络断)。

        Out of scope:本地 inbox counter UI(不读 inbound ack)。
        """
        ...

    async def forward_messages(
        self, from_chat_id: int, to_chat_id: int, msg_ids: list[int]
    ) -> None:
        """2026-09-09 v1.7.2:批量转发消息。

        `from_chat_id` / `to_chat_id` 是 Telegram chat id(频道 id 或私聊 id);
        `msg_ids` 长度上限 100,且必须 sorted 递增(API 约束)。
        实现内部 `sorted()` 兜底,UI 可任意顺序传入。失败抛
        `TelegramAPIError`,调用方走 per-item 异常隔离。
        """
        ...

    async def pin_messages(
        self, channel_id: int, msg_ids: list[int], *, only_for_self: bool = True
    ) -> None:
        """2026-09-09 v1.7.2:批量钉选 — per-message RPC 逐条发。

        `only_for_self=True` 默认只影响本端(不影响其它客户端显示);
        `msg_ids` 空 list = no-op。
        """
        ...

    async def unpin_messages(self, channel_id: int, msg_ids: list[int]) -> None:
        """2026-09-09 v1.7.2:批量取消钉选 — per-message RPC 逐条发。"""
        ...

    async def add_reaction(
        self,
        channel_id: int,
        msg_id: int,
        reaction: str,
        *,
        is_big: bool = False,
    ) -> None:
        """emoji 回应 — per-message RPC,不支持批量;调用方走循环 + 异常隔离。

        `reaction` 两种格式(2026-09-10 v1.7.3):
        - 普通 emoji char:`"😀"` `"🔥"` 等 → ReactionEmoji
        - custom emoji:`"custom_emoji_id:<id>"` →
          ReactionCustomEmoji(Telegram Premium 自定义 emoji)
        """
        ...

    async def remove_reaction(self, channel_id: int, msg_id: int, reaction: str) -> None:
        """取消 emoji 回应。

        2026-09-10 v1.7.3:`reaction` 格式与 `add_reaction` 对齐 — 支持
        `"custom_emoji_id:<id>"` 前缀 dispatch ReactionCustomEmoji。
        """
        ...


class UpdateStream:
    """实时更新流的简单封装(协议方法),由实现返回。

    实现必须保证:
      - `aclose()` 是幂等的(重复调不报错)
      - `aclose()` 后 `__anext__` 抛 `StopAsyncIteration`
      - `aclose()` 会**自动**从 client 侧的 `_streams` 拿掉自己,避免
        长会话该列表只增不减导致内存泄漏
    """

    def __aiter__(self) -> AsyncIterator[MessageDTO]:  # type: ignore[empty-body]
        """async iterator protocol — `async for msg in stream:` 入口。"""
        ...

    async def __anext__(self) -> MessageDTO:  # type: ignore[empty-body]
        """取下一个更新;`aclose()` 后抛 `StopAsyncIteration`。

        实现类(如 `_TelethonUpdateStream`)负责;这里声明类型给 mypy
        (调用方 `anext(stream)` 依赖该协议)。
        """
        ...

    async def aclose(self) -> None:
        """关闭流 — 幂等;触发后 `__anext__` 抛 `StopAsyncIteration`,
        同时自动从 client 侧 `_streams` 拿掉自己(防长会话内存泄漏)。
        """
