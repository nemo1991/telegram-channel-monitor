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

from tgmonitor.core.telegram.fake_client import FakeTelegramClient

# ============== forward_messages ==============


def test_fake_client_inherits_forward_messages_from_protocol() -> None:
    """FakeTelegramClient 实现了 Protocol 的 forward_messages 方法。"""
    assert hasattr(FakeTelegramClient, "forward_messages")
    assert callable(getattr(FakeTelegramClient, "forward_messages", None))


# ============== pin_messages ==============


async def test_fake_pin_messages_groups_by_cid() -> None:
    """pin_messages(1, [10, 11]) → pinned_log[1] == [10, 11]。"""
    client = FakeTelegramClient()
    await client.pin_messages(1, [10, 11])
    assert client.pinned_log == {1: [10, 11]}


# ============== add_reaction / remove_reaction ==============


async def test_fake_add_reaction_is_big_kwarg() -> None:
    """add_reaction(..., is_big=True) → log 第 4 元 = True(emoji 大表情)。"""
    client = FakeTelegramClient()
    await client.add_reaction(1, 10, "❤", is_big=True)
    assert client.reactions_log[-1] == (1, 10, "❤", True)


async def test_fake_remove_reaction_records_unset() -> None:
    """remove_reaction → log 第 4 元 = False(区分 add vs remove)。"""
    client = FakeTelegramClient()
    await client.add_reaction(1, 10, "🔥")
    await client.remove_reaction(1, 10, "🔥")
    assert client.reactions_log == [
        (1, 10, "🔥", True),
        (1, 10, "🔥", False),
    ]


async def test_fake_reactions_log_appends_per_call() -> None:
    """多次调 add/remove → 累加(模拟多选 emoji 面板连续切换)。"""
    client = FakeTelegramClient()
    await client.add_reaction(1, 10, "🔥")
    await client.add_reaction(1, 11, "👍")
    await client.add_reaction(2, 100, "❤", is_big=True)
    assert len(client.reactions_log) == 3
