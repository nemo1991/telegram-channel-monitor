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
    async def login(self, phone: str) -> str:
        """发起登录,返回当前状态(phone_required/code_required/password_required/ready)。"""
        ...

    async def submit_code(self, code: str) -> str:
        """提交短信/应用内验证码。返回新状态。"""
        ...

    async def submit_password(self, password: str) -> str:
        """提交 2FA 密码。返回 'ready' 即登录成功。"""
        ...

    async def logout(self) -> None: ...

    @property
    def state(self) -> str: ...

    @property
    def me(self) -> dict | None:
        """当前登录用户 {id, username, first_name, ...};未登录时 None。"""
        ...

    # ---- 频道 ----
    async def list_joined_channels(self) -> list[ChannelDTO]: ...

    async def join_channel(self, identifier: str) -> ChannelDTO:
        """identifier: @username 或 t.me/... 链接。"""
        ...

    # ---- 消息流 ----
    async def iter_messages(
        self, channel_id: int, *, from_msg_id: int = 0, limit: int | None = None
    ) -> AsyncIterator[MessageDTO]:
        """历史回放(若需)。"""
        ...

    def subscribe_updates(self) -> UpdateStream:
        """实时更新订阅,返回 AsyncIterator 形式,生命周期内持续 yield 消息 DTO。"""
        ...


class UpdateStream:
    """实时更新流的简单封装(协议方法),由实现返回。"""

    def __aiter__(self) -> AsyncIterator[MessageDTO]: ...
    async def aclose(self) -> None: ...
