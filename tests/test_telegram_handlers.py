"""6 类新 TDLib chat update handler 单测 — 2026-09-03 v1.6.0 PR #Q2。

覆盖:
- `_on_chat_title` / `_on_chat_photo` / `_on_chat_permissions` /
  `_on_chat_read_inbox` / `_on_chat_read_outbox` / `_on_chat_default_banned_rights`
- 关键:`_on_chat_title` / `_on_chat_photo` 必须 publish 事件;其他 4 类
  只 log,不抛异常(用于「未知 update 静默丢失」兜底回归)。

不测试 `_on_channel_updated` / `_on_supergroup_updated` / `_on_delete_messages`
等既有路径 — 它们已有 v1.4.0 PR #14 / PR #11 单测 / 间接测覆盖,本文件
聚焦 PR #Q2 新增。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tgmonitor.core.events import (
    ChannelPhotoChanged,
    ChannelTitleChanged,
    EventBus,
)

# ---- 假 client + event bus helper ----


class _FakeTdlibObject:
    """模仿 TDLib Json 对象 — 支持 `getattr(obj, "field", None)` 模式。

    `update.field` 在真 TDLib 是 Object 类型;此处我们让它等同 dict 行为,
    handler 内部走 `getattr(update, ..., None)` 双兼容。
    """

    def __init__(self, **fields: Any) -> None:
        for k, v in fields.items():
            setattr(self, k, v)


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def fake_client(bus: EventBus) -> MagicMock:
    """mock 一个 TdlibTelegramClient 实例,只暴露 handler 依赖的最小字段。

    2026-10-10 v1.12.x plan-batch2 目标 3 step 1:events handler 抽到
    `EventsHandler` composition delegate。fixture 改持真 `EventsHandler`
    实例(handler 真实实现),但 mock client 自身状态(`_bus` 等),让
    测试既能验证 handler 行为,又不必初始化完整 TdlibTelegramClient。
    """
    from tgmonitor.core.telegram.tdlib_events import EventsHandler

    client = MagicMock()
    client._bus = bus
    client._streams = []  # _on_new_message / _on_message_edited 用
    client._seen_error_codes = []
    client._last_error_message = ""
    # 装真 EventsHandler,handler 内部走 client._bus / client._streams 真实发事件
    client._events = EventsHandler(client)  # type: ignore[arg-type]
    # `_on_any_update` 是闭包(原 __init__ 末尾构造),测试场景不用,这里跳过
    return client


# ---- 测试 ----


async def test_on_chat_title_emits_event(fake_client: MagicMock, bus: EventBus) -> None:
    """`_on_chat_title` 必须 publish `ChannelTitleChanged` 事件。"""
    # 导入被测函数所在模块

    received: list[ChannelTitleChanged] = []

    async def _on(e: ChannelTitleChanged) -> None:
        received.append(e)

    bus.subscribe(ChannelTitleChanged, _on)

    update = _FakeTdlibObject(chat_id=12345, title="新频道名")
    # handler 签名 (self, client_self, update) — fake_client 同时充当 self 和 client_self
    await fake_client._events._on_chat_title(fake_client, update)
    # PR 1b:`bus.publish_async` 注册 task 到 `bus._inflight`,`flush()` 等投递完成。
    await bus.flush()
    assert len(received) == 1
    assert received[0].channel_id == 12345
    assert received[0].new_title == "新频道名"


async def test_on_chat_title_invalid_payload_no_event(
    fake_client: MagicMock, bus: EventBus
) -> None:
    """缺 chat_id / title → 不发事件,handler 静默吞掉。"""

    received: list[ChannelTitleChanged] = []

    async def _on(e: ChannelTitleChanged) -> None:
        received.append(e)

    bus.subscribe(ChannelTitleChanged, _on)

    # 缺 title
    update = _FakeTdlibObject(chat_id=12345)
    await fake_client._events._on_chat_title(fake_client, update)
    # 缺 chat_id
    update2 = _FakeTdlibObject(title="孤 title")
    await fake_client._events._on_chat_title(fake_client, update2)
    await bus.flush()
    assert received == []


async def test_on_chat_photo_with_path_emits_event(fake_client: MagicMock, bus: EventBus) -> None:
    """`_on_chat_photo` 有 path → publish `ChannelPhotoChanged(local_path=...)`。"""

    received: list[ChannelPhotoChanged] = []

    async def _on(e: ChannelPhotoChanged) -> None:
        received.append(e)

    bus.subscribe(ChannelPhotoChanged, _on)

    photo = {"local": {"path": "/tmp/tdlib/photo_12345.jpg"}}
    update = _FakeTdlibObject(chat_id=12345, photo=photo)
    await fake_client._events._on_chat_photo(fake_client, update)
    await bus.flush()
    assert len(received) == 1
    assert received[0].channel_id == 12345
    assert received[0].local_path == "/tmp/tdlib/photo_12345.jpg"


async def test_on_chat_photo_none_emits_event_with_none(
    fake_client: MagicMock, bus: EventBus
) -> None:
    """`_on_chat_photo` photo=None(头像被删)→ publish local_path=None。"""

    received: list[ChannelPhotoChanged] = []

    async def _on(e: ChannelPhotoChanged) -> None:
        received.append(e)

    bus.subscribe(ChannelPhotoChanged, _on)

    update = _FakeTdlibObject(chat_id=12345, photo=None)
    await fake_client._events._on_chat_photo(fake_client, update)
    await bus.flush()
    assert len(received) == 1
    assert received[0].channel_id == 12345
    assert received[0].local_path is None


async def test_on_chat_permissions_does_not_raise(fake_client: MagicMock) -> None:
    """`_on_chat_permissions` 仅 log,handler 调用不抛。"""

    update = _FakeTdlibObject(chat_id=1, permissions={"can_send_messages": True})
    await fake_client._events._on_chat_permissions(fake_client, update)


async def test_on_chat_read_inbox_does_not_raise(fake_client: MagicMock) -> None:
    """`_on_chat_read_inbox` 仅 log,handler 调用不抛。"""

    update = _FakeTdlibObject(chat_id=1, last_read_inbox_message_id=999)
    await fake_client._events._on_chat_read_inbox(fake_client, update)


async def test_on_chat_read_outbox_does_not_raise(fake_client: MagicMock) -> None:
    """`_on_chat_read_outbox` 仅 log,handler 调用不抛。"""

    update = _FakeTdlibObject(chat_id=1, last_read_outbox_message_id=42)
    await fake_client._events._on_chat_read_outbox(fake_client, update)


async def test_on_chat_default_banned_rights_does_not_raise(
    fake_client: MagicMock,
) -> None:
    """`_on_chat_default_banned_rights` 仅 log,handler 调用不抛。"""

    update = _FakeTdlibObject(chat_id=1, default_banned_rights={"can_send_messages": False})
    await fake_client._events._on_chat_default_banned_rights(fake_client, update)


async def test_handlers_silent_on_broken_update(fake_client: MagicMock) -> None:
    """任何 6 类 handler 接 garbage update 都不抛(防 TDLib 反序列化异常上浮)。"""

    # None update — getattr 会抛,但 handler 应吞掉
    await fake_client._events._on_chat_title(fake_client, None)
    await fake_client._events._on_chat_photo(fake_client, None)
    await fake_client._events._on_chat_permissions(fake_client, None)
    await fake_client._events._on_chat_read_inbox(fake_client, None)
    await fake_client._events._on_chat_read_outbox(fake_client, None)
    await fake_client._events._on_chat_default_banned_rights(fake_client, None)


# ============================================================
# 2026-09-10 v1.7.3:`_on_message_pin_changed` 单测
# ============================================================


async def test_on_message_pin_changed_publishes_event(
    fake_client: MagicMock, bus: EventBus
) -> None:
    """v1.7.3:`_on_message_pin_changed` 必须 publish `MessagePinChanged`(True / False 都发)。"""
    from tgmonitor.core.events import MessagePinChanged

    received: list[MessagePinChanged] = []

    async def _on(e: MessagePinChanged) -> None:
        received.append(e)

    bus.subscribe(MessagePinChanged, _on)

    update = _FakeTdlibObject(chat_id=12345, message_id=99, is_pinned=True)
    await fake_client._events._on_message_pin_changed(fake_client, update)
    await bus.flush()
    assert len(received) == 1
    assert received[0].channel_id == 12345
    assert received[0].telegram_msg_id == 99
    assert received[0].is_pinned is True

    # False 路径
    update2 = _FakeTdlibObject(chat_id=12345, message_id=100, is_pinned=False)
    await fake_client._events._on_message_pin_changed(fake_client, update2)
    await bus.flush()
    assert len(received) == 2
    assert received[1].is_pinned is False


async def test_on_message_pin_changed_missing_fields_silent(
    fake_client: MagicMock, bus: EventBus
) -> None:
    """v1.7.3:缺 chat_id / message_id / is_pinned → handler 静默吞,不 publish。"""
    from tgmonitor.core.events import MessagePinChanged

    received: list[MessagePinChanged] = []

    async def _on(e: MessagePinChanged) -> None:
        received.append(e)

    bus.subscribe(MessagePinChanged, _on)

    # 缺 chat_id
    update = _FakeTdlibObject(message_id=1, is_pinned=True)
    await fake_client._events._on_message_pin_changed(fake_client, update)
    await bus.flush()
    assert received == []

    # 缺 is_pinned
    update2 = _FakeTdlibObject(chat_id=100, message_id=1)
    await fake_client._events._on_message_pin_changed(fake_client, update2)
    await bus.flush()
    assert received == []


async def test_on_message_pin_changed_no_bus_no_raise(fake_client: MagicMock) -> None:
    """v1.7.3:bus 为 None → handler 不抛,asyncio.create_task 分支跳过。"""

    fake_client._bus = None
    update = _FakeTdlibObject(chat_id=100, message_id=1, is_pinned=True)
    # 不应抛
    await fake_client._events._on_message_pin_changed(fake_client, update)
