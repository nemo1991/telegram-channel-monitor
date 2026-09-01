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
from typing import AsyncIterator, Callable

from tgmonitor.core.dto import ChannelDTO, MessageDTO
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream


class FakeUpdateStream(UpdateStream):
    """Fake 实现:`asyncio.Queue` 后端;`aclose` 幂等 + 自动从 `_streams` 拿掉自己。"""

    def __init__(self, on_close: Callable[[FakeUpdateStream], None] | None = None) -> None:
        """`on_close` = aclose 时回调(给 FakeTelegramClient 摘 list 用)。"""
        self._queue: asyncio.Queue[MessageDTO] = asyncio.Queue()
        self._closed = False
        self._on_close = on_close

    async def push(self, msg: MessageDTO) -> None:
        """推一条消息给 subscriber;closed 后静默 no-op。"""
        if not self._closed:
            await self._queue.put(msg)

    def __aiter__(self) -> AsyncIterator[MessageDTO]:
        """async iterator protocol — 返回 self。"""
        return self

    async def __anext__(self) -> MessageDTO:
        """从 queue 拿一条;closed 后抛 StopAsyncIteration。

        与真实流 `_TdlibJsonUpdateStream` 语义一致:`aclose()` 会 `put(None)`
        唤醒阻塞的 `get()`,拿到 None 或已 closed 都应抛 `StopAsyncIteration`,
        不能让 None 漏给消费方(MonitorService._run 会把 None 当流关闭处理)。
        """
        if self._closed:
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is None or self._closed:
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        """幂等关闭:置 `_closed=True` + push sentinel 唤醒等待者 + 调 on_close。"""
        if self._closed:
            return
        self._closed = True
        # 唤醒等待者
        await self._queue.put(None)  # type: ignore[arg-type]
        if self._on_close is not None:
            try:
                self._on_close(self)
            except Exception:  # noqa: BLE001
                pass


class FakeTelegramClient(TelegramClient):
    """纯内存,可注入 channel / message 触发更新。"""

    def __init__(self) -> None:
        """初始 state=`phone_required`;可用 set_* / simulate_incoming 注入行为。"""
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
        """旧版 Protocol 接口 — 保留向后兼容,内部转发到 `submit_phone`.

        返回 `submit_phone` 的 state 部分,丢掉 detail(旧接口只承诺 state)。
        """
        state, _detail = await self.submit_phone(phone)
        return state

    async def submit_phone(self, phone: str) -> tuple[str, str | None]:
        """切到 `code_required`;Fake 不发真 SMS。"""
        self._state = "code_required"
        return self._state, None

    async def start(self) -> tuple[str, str | None]:
        """Fake 已经"启动"了 — 直接返当前 state(无网络)。"""
        # Fake 已经"启动"了 — 直接返回状态
        return self._state, None

    async def nuke_and_rebuild(self, rotate_key: bool = False) -> None:
        """Fake 重置:state 回到 `phone_required`(rotate_key 忽略)。"""
        self._state = "phone_required"

    async def submit_code(self, code: str) -> tuple[str, str | None]:
        """`code="00000"` 走 2FA 分支;其它进 `ready`。

        测试约定:00000 = 模拟触发 2FA。
        """
        if code == "00000":
            self._state = "password_required"
        else:
            self._state = "ready"
            self._me = {"id": 1, "username": "fake", "first_name": "Fake"}
        return self._state, None

    async def submit_password(self, password: str) -> tuple[str, str | None]:
        """Fake 2FA 永远成功 → `ready`。"""
        self._state = "ready"
        self._me = {"id": 1, "username": "fake", "first_name": "Fake"}
        return self._state, None

    async def submit_email(self, email: str) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:Fake email 提交 → `email_code_required`(模拟
        已有账号)或 `registration_required`(模拟新账号)。

        测试约定:含 `+new` 子串 → `registration_required`(模拟新用户),
        否则 → `email_code_required`(模拟已存在账号)。
        """
        if "+new" in email:
            self._state = "registration_required"
        else:
            self._state = "email_code_required"
        return self._state, None

    async def submit_email_code(self, code: str) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:Fake 邮箱验证码永远成功 → `ready`。"""
        self._state = "ready"
        self._me = {"id": 1, "username": "fake", "first_name": "Fake"}
        return self._state, None

    async def submit_registration(
        self, first_name: str, last_name: str = ""
    ) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:Fake 注册永远成功 → `ready`。"""
        self._state = "ready"
        self._me = {
            "id": 1,
            "username": "fake_new",
            "first_name": first_name or "Fake",
            "last_name": last_name or "",
        }
        return self._state, None

    async def logout(self) -> None:
        """Fake 登出:state 回到 `phone_required` + 清 me。"""
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
        """当前顶层状态(继承自 TelegramClient Protocol)。"""
        return self._state

    @property
    def me(self) -> dict | None:
        """当前登录用户 dict;未登录 None。"""
        return self._me

    # ---- 频道 ----
    def add_channel(self, channel: ChannelDTO) -> None:
        """直接注入一条频道到内部 `_channels`。"""
        self._channels[channel.id] = channel

    async def list_joined_channels(self) -> list[ChannelDTO]:
        """返所有 add_channel 注入的频道。"""
        return list(self._channels.values())

    async def join_channel(self, identifier: str) -> ChannelDTO:
        """Fake join:由 identifier hash 出一个稳定 cid,直接放进 `_channels`。"""
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
    async def download_file(
        self,
        file_id: str,
        *,
        progress_callback=None,
    ) -> bytes | None:
        """Fake 下载:返回 `set_download(file_id, ...)` 注入的 bytes。

        - set_download(file_id, bytes):返这些 bytes(模拟成功)。
        - set_download(file_id, None):返 None(模拟下载失败)。
        - 都没注入过 → KeyError → 走 `self._downloads.get(file_id)` 默认 None。
        - `await asyncio.sleep(0)` 让出 loop,模仿真网络 round-trip。

        `progress_callback`(2026-09-01 v1.5.1 PR #B3):若传,模拟 fake
        多 chunk 进度 — 每 ~30% 调一次 `(downloaded, total)`,完成时
        调 `(len(data), len(data))`。测试可由此断言 N 次回调 + 终值。
        """
        await asyncio.sleep(0)
        data = self._downloads.get(file_id)
        if data is not None and progress_callback is not None:
            total = len(data)
            # 模拟多 chunk:0% → 33% → 67% → 100%(4 次回调,含终值)
            for pct in (0, total // 3, 2 * total // 3):
                if pct > 0:
                    await progress_callback(pct, total)
            await progress_callback(total, total)
        return data

    async def iter_chat_history(
        self,
        channel_id: int,
        *,
        before_msg_id: int = 0,
        limit: int = 100,
    ) -> AsyncIterator[MessageDTO]:
        """Fake 全量同步分页历史:模拟"从 before_msg_id 之前拉到最早"。

        - max_id 来自注入的 `set_history(channel_id, max_id, count)`;count 条
          按升序 telegram_msg_id 排,以"before_msg_id 之前"为起点 yield。
        - 每次 yield 后 `await asyncio.sleep(0)` 让出 loop,模拟网络。
        - 支持 inject 错误:`raise_after_n_messages` → 第 N+1 条 yield 前抛
          `TelegramRateLimitError`。

        语义跟 tdlib_client 一致:**只能向旧方向拉**(id 递减),不模拟"向新拉"。
        """
        ch_state = self._history_state.get(channel_id)
        if ch_state is None:
            return  # 没注入过历史,空
        max_id, count = ch_state
        # 起始 id:before_msg_id=0 → 拉最新 count 条(max_id-count+1 ... max_id);
        # before_msg_id>0 → 从其前一条(before_msg_id-1 或更早)开始。
        start = max(1, max_id - count + 1) if before_msg_id == 0 else max(1, before_msg_id - 1)
        end = max_id
        for yielded, mid in enumerate(range(start, end + 1)):
            if self._raise_after_n is not None and yielded == self._raise_after_n:
                self._raise_after_n = None
                from tgmonitor.core.telegram.tdlib_errors import (
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
        """开一个新的 FakeUpdateStream(注册到 `_all_streams` 便于 simulate_incoming)。"""
        s = FakeUpdateStream(on_close=self._remove_stream)
        self._all_streams.append(s)
        return s

    def _remove_stream(self, s: FakeUpdateStream) -> None:
        try:
            self._all_streams.remove(s)
        except ValueError:
            pass  # close() 路径已清空

    # ---- 测试辅助 ----
    async def simulate_incoming(self, msg: MessageDTO) -> None:
        """广播一条 message 到所有打开的 stream(测试实时更新用)。"""
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
