"""TelegramClient — 业务侧接口(Protocol)。

唯一接触 TDLib 的 `core/telegram` 子包把 TDLib 封装在这里,
其他模块只见这层接口,不见 TDLib 类型。

实现见 `tdlib_client.py`;UI / 测试用 `FakeTelegramClient`。
"""
from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from tgmonitor.core.dto import ChannelDTO, MessageDTO


@runtime_checkable
class TelegramClient(Protocol):
    """业务侧唯一的 Telegram 客户端接口。"""

    # ---- 鉴权 ----
    async def start(self) -> tuple[str, str | None]:
        """应用启动入口。返回 (state, detail)。state ∈ {ready, phone_required, error, ...}。"""
        ...

    async def nuke_and_rebuild(self, *, rotate_key: bool = False) -> None:
        """清掉 session db(可选旋转加密 key),杀掉内部 TDLib。调用方负责重建。"""
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
        """2026-08-27 v1.4.0 PR #13:`authorizationStateWaitEmailAddress` 时
        提交邮箱地址。TDLib 会推到 `email_code_required`(已有账号)或
        `registration_required`(新账号)。"""
        ...

    async def submit_email_code(self, code: str) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:`authorizationStateWaitEmailCode` 时
        提交邮箱验证码 → 通常推到 `ready`。"""
        ...

    async def submit_registration(
        self, first_name: str, last_name: str = ""
    ) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:`authorizationStateWaitRegistration` 时
        注册新账号;TDLib 推 → `ready`。"""
        ...

    async def logout(self) -> None:
        """登出 — 清掉 session;caller 重建 client。"""
        ...

    async def close(self) -> None:
        """关停 tdlib_json 后台任务 — app exit 时必调,否则 updates_loop 吊着 loop 不放。"""
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

        走 GetChat + GetSupergroup / GetBasicGroup — 修原 list_joined_channels
        元数据 bug:username / member_count 不在 Chat 上,只在 Supergroup /
        BasicGroup 上。ChannelSyncService 用这个拉元数据。
        """
        ...

    # ---- 消息流 ----
    def iter_chat_history(
        self, channel_id: int, *, before_msg_id: int = 0, limit: int = 100
    ) -> AsyncIterator[MessageDTO]:
        """分页拉取频道历史消息(ChannelSyncService 续拉用)。

        **方向约束:TDLib `GetChatHistory.from_message_id` 只能向旧方向拉**(id 递减)。
        即使想"从某条之后正向拉更新",`iter_chat_history` 也**不**支持 — 想
        拿最新消息请调 `subscribe_updates()` 的实时流。

        参数:
          - `before_msg_id=0`:拉该频道最新 N 条(TDLib 反向翻页直到耗尽)
          - `before_msg_id>0`:从 `before_msg_id` 之前(更早的)消息开始拉
            —— 续拉场景把已知的最小 id 传进来,实现会把本批**最后**一条
            (id 最小)的 id 作为下次入参,直到消息耗尽(返回 <limit 条时结束)
        """
        ...

    # ---- 媒体下载 ----
    async def download_file(self, file_id: str) -> bytes | None:
        """下载 TDLib 文件原 bytes;失败 / 超时返 None,**不抛**。

        实现约定(给 MediaDownloader 用的契约):
          - 两步:异步 `DownloadFile` 触发 + `GetFile` 轮询到 `is_downloading_completed`。
          - 失败(网络 / 权限 / 30 min hard cap)→ 返 None,monitor 循环继续。
          - 真拿到 bytes 才返非 None bytes。
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

        实现类(如 `_TdlibJsonUpdateStream`)负责;这里声明类型给 mypy
        (调用方 `anext(stream)` 依赖该协议)。
        """
        ...
    async def aclose(self) -> None:
        """关闭流 — 幂等;触发后 `__anext__` 抛 `StopAsyncIteration`,
        同时自动从 client 侧 `_streams` 拿掉自己(防长会话内存泄漏)。
        """
