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
from datetime import UTC, datetime
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
        # 全量同步测试 hooks
        self._history_state: dict[int, tuple[int, int]] = {}
        self._metadata_override: dict[int, ChannelDTO] = {}
        self._raise_after_n: int | None = None
        # 媒体下载测试 hooks(REVIEW M2.1 接入)
        self._downloads: dict[str, bytes | None] = {}

    # ---- 鉴权 ----
    async def login(self, phone: str) -> str:
        # 旧版 Protocol 接口 — 保留向后兼容,内部转发到 submit_phone
        return await self.submit_phone(phone)

    async def submit_phone(self, phone: str) -> tuple[str, str | None]:
        self._state = "code_required"
        return self._state, None

    async def start(self) -> tuple[str, str | None]:
        # Fake 已经"启动"了 — 直接返回状态
        return self._state, None

    async def nuke_and_rebuild(self, rotate_key: bool = False) -> None:
        self._state = "phone_required"

    async def submit_code(self, code: str) -> tuple[str, str | None]:
        if code == "00000":
            self._state = "password_required"
        else:
            self._state = "ready"
            self._me = {"id": 1, "username": "fake", "first_name": "Fake"}
        return self._state, None

    async def submit_password(self, password: str) -> tuple[str, str | None]:
        self._state = "ready"
        self._me = {"id": 1, "username": "fake", "first_name": "Fake"}
        return self._state, None

    async def logout(self) -> None:
        self._state = "phone_required"
        self._me = None

    async def close(self) -> None:
        """Fake 无资源,只把状态复位 + 关流。"""
        for s in list(self._all_streams):
            try:
                await s.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._all_streams.clear()

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

    async def get_channel_metadata(self, channel_id: int) -> ChannelDTO:
        """Fake:返回 `_channels` 里的元数据,否则 stub。同步用。"""
        if channel_id in self._channels:
            return self._channels[channel_id]
        # 注入过 metadata?(全量同步测试用)
        if channel_id in self._metadata_override:
            return self._metadata_override[channel_id]
        return ChannelDTO(id=channel_id, title=f"#{channel_id}")

    # ---- 消息流 ----
    async def download_file(self, file_id: str) -> bytes | None:
        """Fake 下载:返回 `set_download(file_id, ...)` 注入的 bytes。

        - set_download(file_id, bytes):返这些 bytes(模拟成功)。
        - set_download(file_id, None):返 None(模拟下载失败)。
        - 都没注入过 → KeyError → 走 `self._downloads.get(file_id)` 默认 None。
        - `await asyncio.sleep(0)` 让出 loop,模仿真网络 round-trip。
        """
        await asyncio.sleep(0)
        return self._downloads.get(file_id)

    async def iter_chat_history(
        self,
        channel_id: int,
        *,
        from_msg_id: int = 0,
        limit: int = 100,
    ) -> AsyncIterator[MessageDTO]:
        """Fake 全量同步分页历史:模拟"从 from_msg_id+1 拉到 max_id"。

        - max_id 来自注入的 `set_history(channel_id, max_id, count)`;count 条
          按升序 telegram_msg_id 排,from_msg_id 之后 yield。
        - 每次 yield 后 `await asyncio.sleep(0)` 让出 loop,模拟网络。
        - 支持 inject 错误:`raise_after_n_messages` → 第 N+1 条 yield 前抛
          `TelegramRateLimitError`。
        """
        ch_state = self._history_state.get(channel_id)
        if ch_state is None:
            return  # 没注入过历史,空
        max_id, count = ch_state
        # 起始 id:from_msg_id=0 → 拉最新 count 条(max_id-count+1 ... max_id);
        # from_msg_id>0 → 从 from_msg_id+1 开始。
        start = max(1, max_id - count + 1) if from_msg_id == 0 else from_msg_id + 1
        end = max_id
        for yielded, mid in enumerate(range(start, end + 1)):
            if self._raise_after_n is not None and yielded == self._raise_after_n:
                self._raise_after_n = None
                from tgmonitor.core.telegram.tdlib_client import (
                    TelegramRateLimitError,
                )
                raise TelegramRateLimitError(60.0)
            yield MessageDTO(
                id=mid,
                channel_id=channel_id,
                telegram_msg_id=mid,
                text=f"history-{channel_id}-{mid}",
                date=datetime.now(UTC),
            )
            # 让出 loop,模仿真网络
            await asyncio.sleep(0)

    def subscribe_updates(self) -> UpdateStream:
        s = FakeUpdateStream()
        self._all_streams.append(s)
        return s

    # ---- 测试辅助 ----
    async def simulate_incoming(self, msg: MessageDTO) -> None:
        for s in list(self._all_streams):
            await s.push(msg)

    # ---- 全量同步测试 hooks ----

    def set_history(self, channel_id: int, max_id: int, count: int) -> None:
        """注入"该频道历史有 count 条,最大 id=max_id"。"""
        self._history_state[channel_id] = (max_id, count)

    def set_metadata(self, channel: ChannelDTO) -> None:
        """注入"get_channel_metadata 返回这个"。"""
        self._metadata_override[channel.id] = channel

    def set_download(self, file_id: str, data: bytes | None) -> None:
        """注入"download_file(file_id) 返回 data";data=None 模拟失败。"""
        self._downloads[file_id] = data

    def inject_rate_limit_after(self, n: int) -> None:
        """iter_chat_history 第 n+1 条 yield 前抛 TelegramRateLimitError(60s)。"""
        self._raise_after_n = n
