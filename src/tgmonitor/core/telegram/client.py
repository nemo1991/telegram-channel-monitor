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
        """清掉 session db(可选旋转加密 key),杀掉内部 aiotdlib。调用方负责重建。"""
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

    async def logout(self) -> None: ...

    async def close(self) -> None:
        """关停 aiotdlib 后台任务 — app exit 时必调,否则 updates_loop 吊着 loop 不放。"""
        ...

    # 旧式 — 留给兼容层;新代码用 submit_phone + submit_code。
    async def login(self, phone: str) -> str: ...

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

    async def get_channel_metadata(self, channel_id: int) -> ChannelDTO:
        """拉取频道的最新元数据(title/username/member_count/kind)。

        走 GetChat + GetSupergroup / GetBasicGroup — 修原 list_joined_channels
        元数据 bug:username / member_count 不在 Chat 上,只在 Supergroup /
        BasicGroup 上。ChannelSyncService 用这个拉元数据。
        """
        ...

    # ---- 消息流 ----
    def iter_chat_history(
        self, channel_id: int, *, from_msg_id: int = 0, limit: int = 100
    ) -> AsyncIterator[MessageDTO]:
        """分页拉取频道历史消息(ChannelSyncService 续拉用)。

        from_msg_id=0 表示"最新 N 条",>0 表示"从 from_msg_id 之后正向拉"。
        返回的迭代器分页自动推进,直到消息耗尽(返回 <limit 条时结束)。
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
        """实时更新订阅,返回 AsyncIterator 形式,生命周期内持续 yield 消息 DTO。"""
        ...


class UpdateStream:
    """实时更新流的简单封装(协议方法),由实现返回。"""

    def __aiter__(self) -> AsyncIterator[MessageDTO]: ...
    async def aclose(self) -> None: ...
