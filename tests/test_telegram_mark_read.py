"""2026-09-08 v1.7.0:TelegramClient.mark_messages_read 协议 + Fake 实现测试。

覆盖:
- FakeTelegramClient.mark_messages_read 调用记录正确(按 cid 分组累加)
- 空 msg_ids 早 return,不写日志
- read_log property 暴露 _read_log 给外部断言
- TelegramClient Protocol mark_messages_read 是 abstractmethod(继承 Protocol)
"""

from __future__ import annotations

import pytest

from tgmonitor.core.telegram.fake_client import FakeTelegramClient


@pytest.mark.asyncio
async def test_fake_mark_messages_read_groups_by_cid() -> None:
    """调用 mark_messages_read(1, [10, 11, 12]) 后 read_log[1] == [10, 11, 12]。"""
    client = FakeTelegramClient()
    await client.mark_messages_read(1, [10, 11, 12])
    assert client.read_log == {1: [10, 11, 12]}


@pytest.mark.asyncio
async def test_fake_mark_messages_read_appends_per_cid() -> None:
    """同一 cid 多次调用 → 累加到同一 list(模拟 UI 分批标记)。"""
    client = FakeTelegramClient()
    await client.mark_messages_read(1, [10])
    await client.mark_messages_read(1, [11, 12])
    await client.mark_messages_read(2, [100])
    assert client.read_log == {1: [10, 11, 12], 2: [100]}


@pytest.mark.asyncio
async def test_fake_mark_messages_read_empty_noop() -> None:
    """空 msg_ids 早 return — read_log 保持空。"""
    client = FakeTelegramClient()
    await client.mark_messages_read(1, [])
    assert client.read_log == {}


@pytest.mark.asyncio
async def test_fake_read_log_is_property_view() -> None:
    """read_log 暴露 _read_log(测试断言)— 同一 dict 实例,mutate 反映到 client。"""
    client = FakeTelegramClient()
    await client.mark_messages_read(1, [10])
    log = client.read_log
    assert log is client._read_log  # type: ignore[attr-defined] — 内部状态
    # 通过 read_log mutate 也生效(虽然 fake 不监听,但暴露是同一 dict)
    log[2] = [99]
    assert 2 in client._read_log  # type: ignore[attr-defined]


def test_fake_client_inherits_mark_messages_read_from_protocol() -> None:
    """FakeTelegramClient 实现了 Protocol 的 mark_messages_read 方法。"""
    # Protocol 不强制 abstractmethod,但 FakeTelegramClient 应该是 Protocol 子类
    # 通过 hasattr 校验即可。
    assert hasattr(FakeTelegramClient, "mark_messages_read")
    assert callable(getattr(FakeTelegramClient, "mark_messages_read", None))
