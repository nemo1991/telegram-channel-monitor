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

import asyncio
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
    """mock 一个 TdlibTelegramClient 实例,只暴露 handler 依赖的最小字段。"""
    client = MagicMock()
    client._bus = bus

    async def _real_safe_publish_pin(chat_id: int, msg_id: int, is_pinned: bool) -> None:
        """2026-09-10 v1.7.3:真实 publish 到 bus(替换 MagicMock 默认 Mock,
        否则 asyncio.create_task 拿到 Mock 而非 coroutine 抛 TypeError)。
        """
        from tgmonitor.core.events import MessagePinChanged

        await bus.publish(
            MessagePinChanged(channel_id=chat_id, telegram_msg_id=msg_id, is_pinned=is_pinned)
        )

    client._safe_publish_pin_changed = _real_safe_publish_pin  # type: ignore[attr-defined]
    return client


# ---- 测试 ----


async def test_on_chat_title_emits_event(fake_client: MagicMock, bus: EventBus) -> None:
    """`_on_chat_title` 必须 publish `ChannelTitleChanged` 事件。"""
    # 导入被测函数所在模块
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient

    received: list[ChannelTitleChanged] = []

    async def _on(e: ChannelTitleChanged) -> None:
        received.append(e)

    bus.subscribe(ChannelTitleChanged, _on)

    update = _FakeTdlibObject(chat_id=12345, title="新频道名")
    # handler 签名 (self, client_self, update) — fake_client 同时充当 self 和 client_self
    await TdlibTelegramClient._on_chat_title(fake_client, fake_client, update)
    # 等 in-flight publish task 完成
    await asyncio.sleep(0.05)
    assert len(received) == 1
    assert received[0].channel_id == 12345
    assert received[0].new_title == "新频道名"


async def test_on_chat_title_invalid_payload_no_event(
    fake_client: MagicMock, bus: EventBus
) -> None:
    """缺 chat_id / title → 不发事件,handler 静默吞掉。"""
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient

    received: list[ChannelTitleChanged] = []

    async def _on(e: ChannelTitleChanged) -> None:
        received.append(e)

    bus.subscribe(ChannelTitleChanged, _on)

    # 缺 title
    update = _FakeTdlibObject(chat_id=12345)
    await TdlibTelegramClient._on_chat_title(fake_client, fake_client, update)
    # 缺 chat_id
    update2 = _FakeTdlibObject(title="孤 title")
    await TdlibTelegramClient._on_chat_title(fake_client, fake_client, update2)
    await asyncio.sleep(0.05)
    assert received == []


async def test_on_chat_photo_with_path_emits_event(fake_client: MagicMock, bus: EventBus) -> None:
    """`_on_chat_photo` 有 path → publish `ChannelPhotoChanged(local_path=...)`。"""
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient

    received: list[ChannelPhotoChanged] = []

    async def _on(e: ChannelPhotoChanged) -> None:
        received.append(e)

    bus.subscribe(ChannelPhotoChanged, _on)

    photo = {"local": {"path": "/tmp/tdlib/photo_12345.jpg"}}
    update = _FakeTdlibObject(chat_id=12345, photo=photo)
    await TdlibTelegramClient._on_chat_photo(fake_client, fake_client, update)
    await asyncio.sleep(0.05)
    assert len(received) == 1
    assert received[0].channel_id == 12345
    assert received[0].local_path == "/tmp/tdlib/photo_12345.jpg"


async def test_on_chat_photo_none_emits_event_with_none(
    fake_client: MagicMock, bus: EventBus
) -> None:
    """`_on_chat_photo` photo=None(头像被删)→ publish local_path=None。"""
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient

    received: list[ChannelPhotoChanged] = []

    async def _on(e: ChannelPhotoChanged) -> None:
        received.append(e)

    bus.subscribe(ChannelPhotoChanged, _on)

    update = _FakeTdlibObject(chat_id=12345, photo=None)
    await TdlibTelegramClient._on_chat_photo(fake_client, fake_client, update)
    await asyncio.sleep(0.05)
    assert len(received) == 1
    assert received[0].channel_id == 12345
    assert received[0].local_path is None


async def test_on_chat_permissions_does_not_raise(fake_client: MagicMock) -> None:
    """`_on_chat_permissions` 仅 log,handler 调用不抛。"""
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient

    update = _FakeTdlibObject(chat_id=1, permissions={"can_send_messages": True})
    await TdlibTelegramClient._on_chat_permissions(fake_client, fake_client, update)


async def test_on_chat_read_inbox_does_not_raise(fake_client: MagicMock) -> None:
    """`_on_chat_read_inbox` 仅 log,handler 调用不抛。"""
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient

    update = _FakeTdlibObject(chat_id=1, last_read_inbox_message_id=999)
    await TdlibTelegramClient._on_chat_read_inbox(fake_client, fake_client, update)


async def test_on_chat_read_outbox_does_not_raise(fake_client: MagicMock) -> None:
    """`_on_chat_read_outbox` 仅 log,handler 调用不抛。"""
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient

    update = _FakeTdlibObject(chat_id=1, last_read_outbox_message_id=42)
    await TdlibTelegramClient._on_chat_read_outbox(fake_client, fake_client, update)


async def test_on_chat_default_banned_rights_does_not_raise(
    fake_client: MagicMock,
) -> None:
    """`_on_chat_default_banned_rights` 仅 log,handler 调用不抛。"""
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient

    update = _FakeTdlibObject(chat_id=1, default_banned_rights={"can_send_messages": False})
    await TdlibTelegramClient._on_chat_default_banned_rights(fake_client, fake_client, update)


async def test_handlers_silent_on_broken_update(fake_client: MagicMock) -> None:
    """任何 6 类 handler 接 garbage update 都不抛(防 TDLib 反序列化异常上浮)。"""
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient

    # None update — getattr 会抛,但 handler 应吞掉
    await TdlibTelegramClient._on_chat_title(fake_client, fake_client, None)
    await TdlibTelegramClient._on_chat_photo(fake_client, fake_client, None)
    await TdlibTelegramClient._on_chat_permissions(fake_client, fake_client, None)
    await TdlibTelegramClient._on_chat_read_inbox(fake_client, fake_client, None)
    await TdlibTelegramClient._on_chat_read_outbox(fake_client, fake_client, None)
    await TdlibTelegramClient._on_chat_default_banned_rights(fake_client, fake_client, None)


# ============================================================
# 2026-09-10 v1.7.3:`_on_message_pin_changed` 单测
# ============================================================


async def test_on_message_pin_changed_publishes_event(
    fake_client: MagicMock, bus: EventBus
) -> None:
    """v1.7.3:`_on_message_pin_changed` 必须 publish `MessagePinChanged`(True / False 都发)。"""
    from tgmonitor.core.events import MessagePinChanged
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient

    received: list[MessagePinChanged] = []

    async def _on(e: MessagePinChanged) -> None:
        received.append(e)

    bus.subscribe(MessagePinChanged, _on)

    update = _FakeTdlibObject(chat_id=12345, message_id=99, is_pinned=True)
    await TdlibTelegramClient._on_message_pin_changed(fake_client, fake_client, update)
    await asyncio.sleep(0.05)
    assert len(received) == 1
    assert received[0].channel_id == 12345
    assert received[0].telegram_msg_id == 99
    assert received[0].is_pinned is True

    # False 路径
    update2 = _FakeTdlibObject(chat_id=12345, message_id=100, is_pinned=False)
    await TdlibTelegramClient._on_message_pin_changed(fake_client, fake_client, update2)
    await asyncio.sleep(0.05)
    assert len(received) == 2
    assert received[1].is_pinned is False


async def test_on_message_pin_changed_missing_fields_silent(
    fake_client: MagicMock, bus: EventBus
) -> None:
    """v1.7.3:缺 chat_id / message_id / is_pinned → handler 静默吞,不 publish。"""
    from tgmonitor.core.events import MessagePinChanged
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient

    received: list[MessagePinChanged] = []

    async def _on(e: MessagePinChanged) -> None:
        received.append(e)

    bus.subscribe(MessagePinChanged, _on)

    # 缺 chat_id
    update = _FakeTdlibObject(message_id=1, is_pinned=True)
    await TdlibTelegramClient._on_message_pin_changed(fake_client, fake_client, update)
    await asyncio.sleep(0.05)
    assert received == []

    # 缺 is_pinned
    update2 = _FakeTdlibObject(chat_id=100, message_id=1)
    await TdlibTelegramClient._on_message_pin_changed(fake_client, fake_client, update2)
    await asyncio.sleep(0.05)
    assert received == []


async def test_on_message_pin_changed_no_bus_no_raise(fake_client: MagicMock) -> None:
    """v1.7.3:bus 为 None → handler 不抛,asyncio.create_task 分支跳过。"""
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient

    fake_client._bus = None
    update = _FakeTdlibObject(chat_id=100, message_id=1, is_pinned=True)
    # 不应抛
    await TdlibTelegramClient._on_message_pin_changed(fake_client, fake_client, update)
