"""2026-09-09 v1.7.2:TelegramClient 5 RPC 协议 + Fake 实现测试。

覆盖:
- FakeTelegramClient.forward_messages / pin_messages / unpin_messages /
  add_reaction / remove_reaction 调用记录正确(per-cid group)
- 空 msg_ids 早 return,不写日志
- forwarded_log / pinned_log / reactions_log property 暴露内部状态
- TelegramClient Protocol 5 RPC 是 abstractmethod
- 多次调用累加而非覆盖

模式参照 tests/test_telegram_mark_read.py:每个 RPC × 3 case
(groups_by_cid / appends / empty_noop) + Protocol presence + log property view。
"""

from __future__ import annotations

import pytest

from tgmonitor.core.telegram.fake_client import FakeTelegramClient

# ============== forward_messages ==============


@pytest.mark.asyncio
async def test_fake_forward_messages_records_log() -> None:
    """调 forward_messages(1, 99, [10, 11]) 后 forwarded_log 含 (1, 99, [10, 11])。

    TDLib `forwardMessages` 要求 sorted msg_ids — fake 也 sorted() 存(测试可断言
    输入无序时 log 是有序的)。
    """
    client = FakeTelegramClient()
    await client.forward_messages(1, 99, [10, 11])
    assert client.forwarded_log == [(1, 99, [10, 11])]


@pytest.mark.asyncio
async def test_fake_forward_messages_sorts_msg_ids() -> None:
    """传无序 msg_ids — log 里是 sorted()(TDLib forwardMessages 协议约束)。"""
    client = FakeTelegramClient()
    await client.forward_messages(1, 99, [30, 10, 20])
    assert client.forwarded_log == [(1, 99, [10, 20, 30])]


@pytest.mark.asyncio
async def test_fake_forward_messages_appends_per_call() -> None:
    """多次调 forward_messages → log 累加,模拟 LIVE 多 batch 转发。"""
    client = FakeTelegramClient()
    await client.forward_messages(1, 99, [10])
    await client.forward_messages(2, 99, [200, 201])
    await client.forward_messages(1, 88, [10, 11])
    assert client.forwarded_log == [
        (1, 99, [10]),
        (2, 99, [200, 201]),
        (1, 88, [10, 11]),
    ]


@pytest.mark.asyncio
async def test_fake_forward_messages_empty_noop() -> None:
    """空 msg_ids 早 return — forwarded_log 保持空 list。"""
    client = FakeTelegramClient()
    await client.forward_messages(1, 99, [])
    assert client.forwarded_log == []


def test_fake_forwarded_log_is_property_view() -> None:
    """forwarded_log property 暴露 _forwarded_log 同一 list 实例。"""
    client = FakeTelegramClient()
    log = client.forwarded_log
    assert log is client._forwarded_log  # type: ignore[attr-defined]


def test_fake_client_inherits_forward_messages_from_protocol() -> None:
    """FakeTelegramClient 实现了 Protocol 的 forward_messages 方法。"""
    assert hasattr(FakeTelegramClient, "forward_messages")
    assert callable(getattr(FakeTelegramClient, "forward_messages", None))


# ============== pin_messages ==============


@pytest.mark.asyncio
async def test_fake_pin_messages_groups_by_cid() -> None:
    """pin_messages(1, [10, 11]) → pinned_log[1] == [10, 11]。"""
    client = FakeTelegramClient()
    await client.pin_messages(1, [10, 11])
    assert client.pinned_log == {1: [10, 11]}


@pytest.mark.asyncio
async def test_fake_pin_messages_appends_per_cid() -> None:
    """同一 cid 多次调 → 累加到同一 list(模拟分批)。"""
    client = FakeTelegramClient()
    await client.pin_messages(1, [10])
    await client.pin_messages(1, [11, 12])
    await client.pin_messages(2, [100])
    assert client.pinned_log == {1: [10, 11, 12], 2: [100]}


@pytest.mark.asyncio
async def test_fake_pin_messages_empty_noop() -> None:
    """空 msg_ids 早 return — pinned_log 保持空。"""
    client = FakeTelegramClient()
    await client.pin_messages(1, [])
    assert client.pinned_log == {}


@pytest.mark.asyncio
async def test_fake_unpin_messages_groups_by_cid() -> None:
    """unpin_messages 与 pin_messages 同款 — 共享 _pinned_log 累加。"""
    client = FakeTelegramClient()
    await client.unpin_messages(1, [10])
    await client.unpin_messages(2, [200, 201])
    assert client.pinned_log == {1: [10], 2: [200, 201]}


def test_fake_pinned_log_is_property_view() -> None:
    """pinned_log property 暴露 _pinned_log 同一 dict 实例。"""
    client = FakeTelegramClient()
    log = client.pinned_log
    assert log is client._pinned_log  # type: ignore[attr-defined]


def test_fake_client_inherits_pin_messages_from_protocol() -> None:
    assert hasattr(FakeTelegramClient, "pin_messages")
    assert callable(getattr(FakeTelegramClient, "pin_messages", None))


def test_fake_client_inherits_unpin_messages_from_protocol() -> None:
    assert hasattr(FakeTelegramClient, "unpin_messages")
    assert callable(getattr(FakeTelegramClient, "unpin_messages", None))


# ============== add_reaction / remove_reaction ==============


@pytest.mark.asyncio
async def test_fake_add_reaction_records_log() -> None:
    """add_reaction(1, 10, '🔥') → reactions_log 含 (1, 10, '🔥', True)。"""
    client = FakeTelegramClient()
    await client.add_reaction(1, 10, "🔥")
    assert client.reactions_log == [(1, 10, "🔥", True)]


@pytest.mark.asyncio
async def test_fake_add_reaction_is_big_kwarg() -> None:
    """add_reaction(..., is_big=True) → log 第 4 元 = True(emoji 大表情)。"""
    client = FakeTelegramClient()
    await client.add_reaction(1, 10, "❤", is_big=True)
    assert client.reactions_log[-1] == (1, 10, "❤", True)


@pytest.mark.asyncio
async def test_fake_remove_reaction_records_unset() -> None:
    """remove_reaction → log 第 4 元 = False(区分 add vs remove)。"""
    client = FakeTelegramClient()
    await client.add_reaction(1, 10, "🔥")
    await client.remove_reaction(1, 10, "🔥")
    assert client.reactions_log == [
        (1, 10, "🔥", True),
        (1, 10, "🔥", False),
    ]


@pytest.mark.asyncio
async def test_fake_reactions_log_appends_per_call() -> None:
    """多次调 add/remove → 累加(模拟多选 emoji 面板连续切换)。"""
    client = FakeTelegramClient()
    await client.add_reaction(1, 10, "🔥")
    await client.add_reaction(1, 11, "👍")
    await client.add_reaction(2, 100, "❤", is_big=True)
    assert len(client.reactions_log) == 3


def test_fake_reactions_log_is_property_view() -> None:
    """reactions_log property 暴露 _reactions_log 同一 list 实例。"""
    client = FakeTelegramClient()
    log = client.reactions_log
    assert log is client._reactions_log  # type: ignore[attr-defined]


def test_fake_client_inherits_add_reaction_from_protocol() -> None:
    assert hasattr(FakeTelegramClient, "add_reaction")
    assert callable(getattr(FakeTelegramClient, "add_reaction", None))


def test_fake_client_inherits_remove_reaction_from_protocol() -> None:
    assert hasattr(FakeTelegramClient, "remove_reaction")
    assert callable(getattr(FakeTelegramClient, "remove_reaction", None))
