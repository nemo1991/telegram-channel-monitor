"""Live update path — TDLib updateNewMessage → MonitorService → EventBus。

用户报告(2026-07-21):"频道信息获取只发生在启动时一次,频道有新消息
时,已启动的应用并没有收到。"

怀疑链:
  1. `TdlibTelegramClient._on_new_message` (handler) 没被 dispatch
  2. 派发了但 `_streams` 是空(monitor.start() 时机问题)
  3. MonitorService._run 没在 pump(loop 卡住)
  4. EventBus.publish() 派发了但 MessageView 没订阅
  5. MessageView 订阅了但 append() 路径有问题

这个测试用 stub 的 aiotdlib init,直接调 _on_new_message() + 走完整
MonitorService 流,验证 1/2/3/4/5 都成立。`_on_new_message` 是用
`add_event_handler(API.Types.UPDATE_NEW_MESSAGE, ...)` 注册的,真实
aiotdlib 会在 receive loop 收到 packet 时按 update.ID 派发 — 我们直接
调 `_on_new_message` 等价。
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, Settings
from tgmonitor.core.events import EventBus, MessageReceived
from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.core.telegram import tdlib_client as tdc


class _FakeMsg:
    """`TDLib Message` 的最小可映射 stub(只需要 _map_message 读到的字段)。

    TDLib 的 updateNewMessage 包一层,handler 读 update.message 才拿到
    Message 本身;所以这里有 _FakeUpdateNewMessage 包一层。
    """

    def __init__(self, chat_id: int = 100, msg_id: int = 1, text: str = "hi") -> None:
        self.id = msg_id
        self.chat_id = chat_id
        self.date = int(datetime(2026, 7, 21, 9, 0, 0, tzinfo=UTC).timestamp())
        self.edit_date = 0
        self.is_channel_post = True
        self.author_signature = ""
        # messageText wrapper — 类名必须 = "MessageText" 让 _SERVICE_HANDLERS 命中
        self.content = MessageText(text)


class _FormattedText:
    """模拟 aiotdlib 的 FormattedText pydantic model。"""

    def __init__(self, text: str) -> None:
        self.text = text


# 类名不带下划线 — `type(...).__name__` 会是 "MessageText",_SERVICE_HANDLERS
# 字典里就这一行能命中
class MessageText:  # noqa: N801 — 名字必须跟 TDLib 一致
    """TDLib 的 messageText content 包装。"""

    def __init__(self, text: str) -> None:
        self.text = _FormattedText(text)


class _FakeUpdateNewMessage:
    """包一层,模拟 aiotdlib 的 UpdateNewMessage(update.message = Message)。"""

    def __init__(self, msg: _FakeMsg) -> None:
        self.message = msg


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_id=1, api_hash="x" * 32, phone="+10000000000",
        session_dir=tmp_path / "session",
        db_root=tmp_path / "m",
        objectstore_root=tmp_path / "o",
        media_policy=MediaPolicy.METADATA,
        db_backend=DBBackend.JSONL,
        objectstore_backend=ObjectStoreBackend.LOCAL,
    )


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.mark.asyncio
async def test_on_new_message_pushes_to_subscribed_stream(
    settings, bus, stub_aiotdlib_init,
):
    """最小路径:`_on_new_message` 调起后,`subscribe_updates()` 拿到的 stream
    应该能 `__anext__` 到 MessageDTO。"""
    client = tdc.TdlibTelegramClient(settings, event_bus=bus)
    stream = client.subscribe_updates()

    # 直接调 handler(等价于 aiotdlib receive loop 收到 updateNewMessage)
    update = _FakeUpdateNewMessage(_FakeMsg(chat_id=100, msg_id=42, text="hello world"))
    await client._on_new_message(client, update)

    # 200ms 内 stream 应该能拿到
    try:
        msg = await asyncio.wait_for(stream.__anext__(), timeout=0.2)
    except (TimeoutError, StopAsyncIteration) as e:
        pytest.fail(f"stream 没收到消息: {e!r}")
    assert msg.channel_id == 100
    assert msg.telegram_msg_id == 42
    assert msg.text == "hello world"


@pytest.mark.asyncio
async def test_monitor_service_publishes_message_received(
    settings, bus, stub_aiotdlib_init,
):
    """完整路径:`_on_new_message` → stream → MonitorService._handle → bus.publish。

    用 in-memory storage 接 save_message(避免碰真实 DB)。
    """
    from tests.conftest import InMemoryRepository
    from tgmonitor.core.objectstore.local_store import LocalObjectStore

    storage = InMemoryRepository()
    objects = LocalObjectStore(root=settings.objectstore_root)
    await storage.connect()
    await objects.connect()

    client = tdc.TdlibTelegramClient(settings, event_bus=bus)
    monitor = MonitorService(bus, client, storage, objects, settings)
    monitor.set_whitelist([100])  # 频道 100 在白名单

    # 用 asyncio.Event 等,避免 busy-loop
    done = asyncio.Event()
    captured: list[MessageReceived] = []

    async def _cap(e):
        if isinstance(e, MessageReceived):
            captured.append(e.message)
            done.set()

    bus.subscribe(MessageReceived, _cap)

    # 启 monitor,触发 _run 协程
    await monitor.start()

    # 等 _run 真的进了 async for 循环(队列空时 await queue.get() 让出 CPU)
    await asyncio.sleep(0.05)

    # 调 _on_new_message — 应该推到 stream → _handle → bus.publish
    update = _FakeUpdateNewMessage(_FakeMsg(chat_id=100, msg_id=1, text="live update test"))
    await client._on_new_message(client, update)

    try:
        await asyncio.wait_for(done.wait(), timeout=1.0)
    except TimeoutError:
        await monitor.stop()
        pytest.fail(
            "MessageReceived 没在 1s 内派发 — live update 链路断了。"
            "可能位置:_on_new_message 未触发 / streams 空 / _run 未 pump / bus 未 publish"
        )

    await monitor.stop()

    assert captured, "应该至少捕获到一条 MessageReceived"
    assert captured[0].text == "live update test"
    assert captured[0].channel_id == 100


@pytest.mark.asyncio
async def test_on_new_message_skips_non_whitelisted_channel(
    settings, bus, stub_aiotdlib_init,
):
    """频道不在白名单 → MonitorService 静默 drop,不发 MessageReceived。"""
    from tests.conftest import InMemoryRepository
    from tgmonitor.core.objectstore.local_store import LocalObjectStore

    storage = InMemoryRepository()
    objects = LocalObjectStore(root=settings.objectstore_root)
    await storage.connect()
    await objects.connect()

    client = tdc.TdlibTelegramClient(settings, event_bus=bus)
    monitor = MonitorService(bus, client, storage, objects, settings)
    monitor.set_whitelist([100])  # 不含 200

    received: list[MessageReceived] = []

    async def _cap(e):
        if isinstance(e, MessageReceived):
            received.append(e)

    bus.subscribe(MessageReceived, _cap)

    await monitor.start()
    await asyncio.sleep(0.05)

    # 频道 200 不在白名单
    update = _FakeUpdateNewMessage(_FakeMsg(chat_id=200, msg_id=1, text="ignored"))
    await client._on_new_message(client, update)

    # 等够长让 _run 有机会处理(确认它不会发)
    await asyncio.sleep(0.3)
    await monitor.stop()

    assert not received, "非白名单频道不该派发 MessageReceived"
