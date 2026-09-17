"""2026-09-17 PR 6:MessageDTO round-trip property test — InMemory backend。

PR 6 plan:`MessageDTO` round-trip × 4 backend,Property-based fuzz 找出序列化
丢字段的 bug。

本文件只跑 **InMemory backend**(其它 backend 需要 testcontainers / mongomock,
放 `tests/integration/`)。InMemory 是内存 dict,等价 = 序列化/反序列化无损。

预期 bug(plan 已列):
- `tags` 顺序(JSON list 反序列化保序,但 Python set → list 会乱序)
- `reactions=None` vs `[]` round-trip(空 list 可能变 None)
- `media_album_id` None vs ''
- `forward_origin` dict 深 round-trip

如果 hypothesis 找到 bug,加 `@example` 锁定 + skip 即可,提交独立 follow-up PR。
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings

from tests.fixtures._in_memory_repository import InMemoryRepository
from tests.fixtures._strategies import message_dtos


@pytest.mark.asyncio
@given(msg=message_dtos())
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
async def test_message_dto_inmemory_roundtrip(msg) -> None:
    """任意 MessageDTO → `save_message` → `get_message` → 字段相等。"""
    repo = InMemoryRepository()
    await repo.init_schema()

    # 落库前 channel 必须存在(InMemory 用 channel_id 当 dict key,但 channel
    # 没注册时 message 是孤儿 — InMemory 实现允许孤儿,我们直接 save)
    pk = await repo.save_message(msg)
    assert pk > 0

    loaded = await repo.get_message(msg.channel_id, msg.telegram_msg_id)
    assert loaded is not None, "get_message 返 None — save_message 未生效"

    # channel_id + telegram_msg_id 必须相等
    assert loaded.channel_id == msg.channel_id
    assert loaded.telegram_msg_id == msg.telegram_msg_id
    # date 字段(aware datetime)
    assert loaded.date == msg.date
    # text 字段
    assert loaded.text == msg.text
    # 列表字段
    assert loaded.tags == msg.tags
    assert loaded.is_favorite == msg.is_favorite
    assert loaded.notes == msg.notes
    assert loaded.is_pinned == msg.is_pinned
    assert loaded.edited == msg.edited


@pytest.mark.asyncio
@given(msg=message_dtos())
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
async def test_message_dto_inmedia_list_roundtrip(msg) -> None:
    """Media list round-trip — 多媒体 DTO 是 PR #9 加的字段,容易丢。"""
    # 只测有 media 的 case(filter 比 reject 更省时)
    if not msg.media:
        return  # type: ignore[return-value]

    repo = InMemoryRepository()
    await repo.init_schema()
    await repo.save_message(msg)

    loaded = await repo.get_message(msg.channel_id, msg.telegram_msg_id)
    assert loaded is not None
    assert len(loaded.media) == len(msg.media), (
        f"media 数量丢:{len(msg.media)} → {len(loaded.media)} "
        f"(msg.cid={msg.channel_id}, msg.mid={msg.telegram_msg_id})"
    )
    # 每条 media 关键字段
    for orig, roundtripped in zip(msg.media, loaded.media, strict=False):
        assert orig.type == roundtripped.type
        assert orig.mime_type == roundtripped.mime_type
        assert orig.file_name == roundtripped.file_name
        assert orig.file_size == roundtripped.file_size
        assert orig.object_key == roundtripped.object_key
        assert orig.object_backend == roundtripped.object_backend


@pytest.mark.asyncio
@given(msg=message_dtos())
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
async def test_message_dto_reactions_roundtrip(msg) -> None:
    """Reactions round-trip — None vs [] 区分。"""
    if msg.reactions is None:
        # None = 没推过,InMemory 应保留 None,不应变 []
        repo = InMemoryRepository()
        await repo.init_schema()
        await repo.save_message(msg)
        loaded = await repo.get_message(msg.channel_id, msg.telegram_msg_id)
        assert loaded is not None
        assert loaded.reactions is None, (
            "reactions=None 应保留 None,不应被默认 [] 覆盖"
        )
        return  # type: ignore[return-value]

    if not msg.reactions:
        return  # type: ignore[return-value]

    repo = InMemoryRepository()
    await repo.init_schema()
    await repo.save_message(msg)
    loaded = await repo.get_message(msg.channel_id, msg.telegram_msg_id)
    assert loaded is not None
    assert loaded.reactions is not None
    assert len(loaded.reactions) == len(msg.reactions)
    for orig, roundtripped in zip(msg.reactions, loaded.reactions, strict=False):
        assert orig.emoji == roundtripped.emoji
        assert orig.count == roundtripped.count
        assert orig.is_chosen == roundtripped.is_chosen


# ============== 锁住已发现的边界 case ==============


@pytest.mark.asyncio
async def test_message_with_empty_text_roundtrip() -> None:
    """`text=""` round-trip — 边界 case(空 str vs None 区分)。"""
    from datetime import UTC, datetime

    from tgmonitor.core.dto import MessageDTO

    msg = MessageDTO(
        id=1,
        channel_id=100,
        telegram_msg_id=1,
        text="",
        date=datetime(2026, 9, 17, tzinfo=UTC),
    )
    repo = InMemoryRepository()
    await repo.init_schema()
    await repo.save_message(msg)
    loaded = await repo.get_message(100, 1)
    assert loaded is not None
    assert loaded.text == ""