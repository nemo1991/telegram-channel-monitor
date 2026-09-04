"""UnconfiguredTelegramClient — 凭据未配置时的占位实现。

工厂(`factory.build_telegram_client`)在检测到 `TG_API_ID` / `TG_API_HASH` /
`TG_PHONE` 缺失时返回本占位类,**不**构造真 `TdlibTelegramClient` — 让应用
在没有凭据时也能正常启动进 UI,用户在 设置 → 账户 填好凭据、重启后即可监听。

行为契约:
  - `state` 恒为 `"phone_required"`(UI 显示"未登录"引导,不弹启动失败窗)
  - `start()` 返回 `("phone_required", ...)`,monitor / app_service 视为未登录
  - 频道 / 历史 / 下载接口返回空或 None(best-effort),`join_channel` /
    `get_channel_metadata` 抛 `TelegramNotConfiguredError`(防御直接调用)
  - `subscribe_updates()` 返回一个**永不结束**的流,`aclose()` 唤醒退出 —
    与 `MonitorService.stop()` 的关流流程自洽
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import AsyncIterator, Callable

from tgmonitor.core.dto import ChannelDTO, MessageDTO
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream
from tgmonitor.core.telegram.tdlib_errors import TelegramNotConfiguredError


class _IdleUpdateStream(UpdateStream):
    """永不结束的占位更新流:`aclose` 幂等 + 唤醒 `__anext__` 退出。"""

    def __init__(self, on_close: Callable[[_IdleUpdateStream], None] | None = None) -> None:
        """`on_close` = aclose 时回调(给 client 摘 list 用)。"""
        self._queue: asyncio.Queue[MessageDTO] = asyncio.Queue()
        self._closed = False
        self._on_close = on_close

    def __aiter__(self) -> AsyncIterator[MessageDTO]:
        """async iterator protocol — 返回 self。"""
        return self

    async def __anext__(self) -> MessageDTO:
        """阻塞等待;closed 后抛 StopAsyncIteration。"""
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def aclose(self) -> None:
        """幂等关闭:置 `_closed=True` + push sentinel 唤醒等待者 + 调 on_close。"""
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)  # type: ignore[arg-type]
        if self._on_close is not None:
            try:
                self._on_close(self)
            except Exception:  # noqa: BLE001
                pass


class UnconfiguredTelegramClient(TelegramClient):
    """无凭据占位 client — 所有鉴权 / 数据接口给出安全默认值。"""

    def __init__(self) -> None:
        """初始 state=`phone_required`;不持有任何 TDLib 资源。"""
        self._state = "phone_required"
        self._me: dict | None = None
        self._all_streams: list[_IdleUpdateStream] = []

    # ---- 鉴权 ----
    async def start(self) -> tuple[str, str | None]:
        """返 `phone_required` + 引导文案(与应用状态机一致,不发错误事件)。"""
        return self._state, "未配置 Telegram 凭据,请打开 设置 → 账户 填写"

    async def nuke_and_rebuild(self, rotate_key: bool = False) -> None:
        """无资源可清,no-op。"""
        return None

    async def submit_phone(self, phone: str) -> tuple[str, str | None]:
        """凭据缺失,兜底返 `phone_required`(AuthService 会先拦截)。"""
        return self._state, None

    async def submit_code(self, code: str) -> tuple[str, str | None]:
        """凭据缺失,兜底返 `phone_required`(AuthService 会先拦截)。"""
        return self._state, None

    async def submit_password(self, password: str) -> tuple[str, str | None]:
        """凭据缺失,兜底返 `phone_required`(AuthService 会先拦截)。"""
        return self._state, None

    async def submit_email(self, email: str) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:无凭据时兜底返 `phone_required`,UI 不应走到。"""
        return self._state, None

    async def submit_email_code(self, code: str) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:无凭据时兜底返 `phone_required`,UI 不应走到。"""
        return self._state, None

    async def submit_registration(
        self,
        first_name: str,
        last_name: str = "",
    ) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:无凭据时兜底返 `phone_required`,UI 不应走到。"""
        return self._state, None

    async def logout(self) -> None:
        """未登录,no-op。"""
        return None

    async def close(self) -> None:
        """关掉所有占位流(幂等)。"""
        for s in list(self._all_streams):
            try:
                await s.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._all_streams.clear()

    async def stop(self) -> None:
        """2026-09-03 v1.6.1:未登录状态 stop = no-op(没资源可释放)。"""
        return None

    @property
    def state(self) -> str:
        """恒为 `phone_required` — UI 据此显示未登录引导。"""
        return self._state

    @property
    def me(self) -> dict | None:
        """未登录,恒 None。"""
        return self._me

    # ---- 频道 ----
    async def list_joined_channels(self) -> list[ChannelDTO]:
        """无凭据无法联网,best-effort 返空列表(不抛)。"""
        return []

    async def join_channel(self, identifier: str) -> ChannelDTO:
        """无凭据不可用,抛 `TelegramNotConfiguredError`(防御)。"""
        raise TelegramNotConfiguredError()

    async def get_channel_metadata(self, channel_id: int) -> ChannelDTO:
        """无凭据不可用,抛 `TelegramNotConfiguredError`(防御)。"""
        raise TelegramNotConfiguredError()

    # ---- 消息流 ----
    async def download_file(self, file_id: str, *, progress_callback=None) -> bytes | None:
        """无凭据不可下载,返 None(monitor 循环继续)。

        `progress_callback`(2026-09-01 v1.5.1 PR #B3):与 Protocol 签名
        对齐;无凭据场景无下载可报,不调。
        """
        return None

    async def iter_chat_history(
        self,
        channel_id: int,
        *,
        before_msg_id: int = 0,
        limit: int = 100,
    ) -> AsyncIterator[MessageDTO]:
        """无凭据无历史,空迭代。"""
        if False:  # pragma: no cover — 仅让函数构成 async generator,永不执行
            yield MessageDTO(
                id=0,
                channel_id=channel_id,
                telegram_msg_id=0,
                text="",
                date=datetime.now(UTC),
            )

    def subscribe_updates(self) -> UpdateStream:
        """开一个永不结束的占位流;`aclose()` 唤醒退出。"""
        s = _IdleUpdateStream(on_close=self._remove_stream)
        self._all_streams.append(s)
        return s

    def _remove_stream(self, s: _IdleUpdateStream) -> None:
        try:
            self._all_streams.remove(s)
        except ValueError:
            pass  # close() 路径已清空
