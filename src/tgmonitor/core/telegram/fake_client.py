"""FakeTelegramClient — 测试 / 开发用,无网络,完全可控。

用法:
    client = FakeTelegramClient()
    await client.connect()
    await client.simulate_incoming(MessageDTO(...))
    async for msg in client.subscribe_updates():
        ...
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from tgmonitor.core.dto import ChannelDTO, MessageDTO
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream


class FakeUpdateStream(UpdateStream):
    def __init__(self) -> None:
        self._queue: asyncio.Queue[MessageDTO] = asyncio.Queue()
        self._closed = False

    async def push(self, msg: MessageDTO) -> None:
        if not self._closed:
            await self._queue.put(msg)

    def __aiter__(self) -> AsyncIterator[MessageDTO]:
        return self

    async def __anext__(self) -> MessageDTO:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def aclose(self) -> None:
        self._closed = True
        # 唤醒等待者
        await self._queue.put(None)  # type: ignore[arg-type]


class FakeTelegramClient(TelegramClient):
    """纯内存,可注入 channel / message 触发更新。"""

    def __init__(self) -> None:
        self._state = "phone_required"
        self._me: dict | None = None
        self._channels: dict[int, ChannelDTO] = {}
        self._stream = FakeUpdateStream()
        self._all_streams: list[FakeUpdateStream] = [self._stream]

    # ---- 鉴权 ----
    async def login(self, phone: str) -> str:
        self._state = "code_required"
        return self._state

    async def submit_code(self, code: str) -> str:
        if code == "00000":
            self._state = "password_required"
        else:
            self._state = "ready"
            self._me = {"id": 1, "username": "fake", "first_name": "Fake"}
        return self._state

    async def submit_password(self, password: str) -> str:
        self._state = "ready"
        self._me = {"id": 1, "username": "fake", "first_name": "Fake"}
        return self._state

    async def logout(self) -> None:
        self._state = "phone_required"
        self._me = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def me(self) -> dict | None:
        return self._me

    # ---- 频道 ----
    def add_channel(self, channel: ChannelDTO) -> None:
        self._channels[channel.id] = channel

    async def list_joined_channels(self) -> list[ChannelDTO]:
        return list(self._channels.values())

    async def join_channel(self, identifier: str) -> ChannelDTO:
        # 直接构造一个虚拟频道
        cid = abs(hash(identifier)) % (10**10)
        ch = ChannelDTO(id=cid, title=identifier.lstrip("@"), username=identifier.lstrip("@"))
        self._channels[cid] = ch
        return ch

    # ---- 消息流 ----
    async def iter_messages(
        self, channel_id: int, *, from_msg_id: int = 0, limit: int | None = None
    ) -> AsyncIterator[MessageDTO]:
        for i in range(limit or 0):
            yield MessageDTO(
                id=i,
                channel_id=channel_id,
                telegram_msg_id=from_msg_id + i + 1,
                text=f"history {i}",
            )
            await asyncio.sleep(0)

    def subscribe_updates(self) -> UpdateStream:
        s = FakeUpdateStream()
        self._all_streams.append(s)
        return s

    # ---- 测试辅助 ----
    async def simulate_incoming(self, msg: MessageDTO) -> None:
        for s in list(self._all_streams):
            await s.push(msg)
