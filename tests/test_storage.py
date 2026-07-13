"""InMemoryRepository 单测 — 验证 StorageRepository 抽象的查询语义。"""
from __future__ import annotations

from datetime import datetime, timedelta

from tests.conftest import InMemoryRepository, make_message


async def test_channel_upsert_and_list():
    repo = InMemoryRepository()
    from tgmonitor.core.dto import ChannelDTO

    await repo.upsert_channel(ChannelDTO(id=1, title="a"))
    await repo.upsert_channel(ChannelDTO(id=2, title="b"))
    chs = await repo.list_channels()
    assert [c.id for c in chs] == [1, 2]


async def test_message_idempotent():
    repo = InMemoryRepository()
    m = make_message(msg_id=10)
    pk1 = await repo.save_message(m)
    pk2 = await repo.save_message(m)
    assert pk1 == pk2
    assert await repo.count_messages(100) == 1


async def test_list_messages_sorted_and_filtered():
    repo = InMemoryRepository()
    base = datetime(2026, 1, 1, 12, 0, 0)
    for i, ch in enumerate([1, 2]):
        for j in range(3):
            await repo.save_message(
                make_message(
                    channel_id=ch,
                    msg_id=j,
                    text=f"ch{ch} msg{j}",
                    date=base + timedelta(minutes=i * 10 + j),
                )
            )
    all_msgs = await repo.list_messages([1, 2])
    assert [m.text for m in all_msgs] == [
        "ch1 msg0", "ch1 msg1", "ch1 msg2",
        "ch2 msg0", "ch2 msg1", "ch2 msg2",
    ]
    only_ch1 = await repo.list_messages([1])
    assert all(m.channel_id == 1 for m in only_ch1)


async def test_list_messages_date_range():
    repo = InMemoryRepository()
    base = datetime(2026, 1, 1)
    for d in range(5):
        await repo.save_message(
            make_message(msg_id=d, date=base + timedelta(days=d))
        )
    out = await repo.list_messages([100], date_from=base + timedelta(days=1), date_to=base + timedelta(days=3))
    assert [m.telegram_msg_id for m in out] == [1, 2, 3]


async def test_delete_cascade():
    repo = InMemoryRepository()
    await repo.save_message(make_message(channel_id=7, msg_id=1))
    from tgmonitor.core.dto import ChannelDTO
    await repo.upsert_channel(ChannelDTO(id=7, title="x"))
    await repo.delete_channel(7)
    assert await repo.count_messages(7) == 0
    assert await repo.get_channel(7) is None
