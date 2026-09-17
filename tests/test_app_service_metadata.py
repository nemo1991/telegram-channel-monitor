"""2026-09-17 PR 4 mega-split:`AppService` 元数据 facade 测试(set/list)。

从原 `tests/test_app_service_batch.py` 拆出 — set_favorite / set_tags / set_notes /
list_favorites / list_by_tag 不是 batch op(不调 client RPC,不走 _is_paused guard),
跟 batch facade 分开更清晰。

6 tests:
- set_favorite / set_tags / set_notes:写本地 storage + publish MessageEdited
- set_favorite 找不到 msg 时不 publish(避免 UI 误刷新)
- list_favorites / list_by_tag:透传到 storage,不改值
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from tgmonitor.core.app_service import AppService
from tgmonitor.core.dto import MessageDTO
from tgmonitor.core.events import MessageEdited


async def test_set_favorite_writes_storage_and_publishes(app: AppService, collected: list) -> None:
    """set_favorite → 调 storage.set_favorite + get_message + publish MessageEdited。"""
    msg = MessageDTO(id=1, channel_id=1, telegram_msg_id=10, text="hi", is_favorite=True)
    app._storage.get_message = AsyncMock(return_value=msg)  # type: ignore[attr-defined]

    await app.set_favorite(1, 10, True)

    app._storage.set_favorite.assert_awaited_once_with(1, 10, True)  # type: ignore[attr-defined]
    edited = [e for e in collected if isinstance(e, MessageEdited)]
    assert len(edited) == 1
    assert edited[0].message.is_favorite is True


async def test_set_favorite_message_missing_no_publish(app: AppService, collected: list) -> None:
    """get_message 返 None → 不 publish MessageEdited(避免 UI 误刷新)。"""
    app._storage.get_message = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    await app.set_favorite(1, 10, True)
    assert [e for e in collected if isinstance(e, MessageEdited)] == []


async def test_set_tags_writes_storage_and_publishes(app: AppService, collected: list) -> None:
    msg = MessageDTO(id=1, channel_id=1, telegram_msg_id=10, text="x", tags=["tech", "news"])
    app._storage.get_message = AsyncMock(return_value=msg)  # type: ignore[attr-defined]
    await app.set_tags(1, 10, ["tech", "news"])
    app._storage.set_tags.assert_awaited_once_with(1, 10, ["tech", "news"])  # type: ignore[attr-defined]
    edited = [e for e in collected if isinstance(e, MessageEdited)]
    assert edited[0].message.tags == ["tech", "news"]


async def test_set_notes_writes_storage_and_publishes(app: AppService, collected: list) -> None:
    msg = MessageDTO(id=1, channel_id=1, telegram_msg_id=10, text="x", notes="later")
    app._storage.get_message = AsyncMock(return_value=msg)  # type: ignore[attr-defined]
    await app.set_notes(1, 10, "later")
    app._storage.set_notes.assert_awaited_once_with(1, 10, "later")  # type: ignore[attr-defined]
    edited = [e for e in collected if isinstance(e, MessageEdited)]
    assert edited[0].message.notes == "later"


async def test_list_favorites_delegates_to_storage(app: AppService) -> None:
    """list_favorites 透传到 storage.list_favorites,不改值。"""
    expected = [MessageDTO(id=1, channel_id=1, telegram_msg_id=10, text="x")]
    app._storage.list_favorites = AsyncMock(return_value=expected)  # type: ignore[attr-defined]
    result = await app.list_favorites()
    assert result is expected


async def test_list_by_tag_delegates_to_storage(app: AppService) -> None:
    """list_by_tag 透传到 storage.list_by_tag(tag)。"""
    expected = [MessageDTO(id=1, channel_id=1, telegram_msg_id=10, text="x")]
    app._storage.list_by_tag = AsyncMock(return_value=expected)  # type: ignore[attr-defined]
    result = await app.list_by_tag("tech")
    app._storage.list_by_tag.assert_awaited_once_with("tech")  # type: ignore[attr-defined]
    assert result is expected
