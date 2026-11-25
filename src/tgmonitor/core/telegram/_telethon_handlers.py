"""Telethon 实时事件 handler 实现 — 2026-11-25 P2-N5a。

从 `telethon_client.py`(1057 行)抽出 10 个实时事件 handler 的实现体,
按 PG 端 `_media_pg.py` 的"free function + self 透传"模式组织:

- 宿主 `TelethonClient` 保留 `_on_xxx` 方法名(Telethon `add_event_handler`
  注册 + 测试直接调用都依赖它),方法体改为 1 行薄委托到本模块
- 本模块的 free function 接收 `host: Any`,只访问宿主声明的 8 个能力
  (streams / publish / photo_cache / settings / client / _entity_to_dto /
  _refresh_channel_metadata),不 import 宿主类以避开私有类型循环依赖
  → 无循环依赖,与新后端/新 handler 解耦

行为完全不变:所有 try/except 语义、日志文案、事件字段逐字搬移。
"""

from __future__ import annotations

import logging
from typing import Any

from telethon import types, utils

from tgmonitor.core.events import (
    ChannelMetadataChanged,
    ChannelPhotoChanged,
    ChannelTitleChanged,
    MessageDeleted,
    MessageInteractionsChanged,
    MessagePinChanged,
)
from tgmonitor.core.telegram.telethon_messages import map_message, map_reactions

log = logging.getLogger(__name__)


async def on_new_message(host: Any, event: Any) -> None:
    try:
        msg = event.message
        if msg is None:
            return
        dto = map_message(msg)
        for s in list(host._streams):
            await s.push(dto)
    except Exception:  # noqa: BLE001
        log.exception("NewMessage handling failed")


async def on_message_edited(host: Any, event: Any) -> None:
    try:
        msg = event.message
        if msg is None:
            return
        dto = map_message(msg)
        for s in list(host._streams):
            await s.push(dto)
    except Exception:  # noqa: BLE001
        log.exception("MessageEdited handling failed")


async def on_message_deleted(host: Any, event: Any) -> None:
    try:
        chat_id = event.chat_id
        if chat_id is None:
            return
        for mid in event.deleted_ids or []:
            host._publish(MessageDeleted(channel_id=chat_id, telegram_msg_id=int(mid)))
    except Exception:  # noqa: BLE001
        log.exception("MessageDeleted handling failed")


async def on_chat_action(host: Any, event: Any) -> None:
    """频道改名 / 头像变化(pin 走 Raw UpdatePinned*,避免与此重复)。"""
    try:
        chat_id = event.chat_id
        if chat_id is None:
            return
        if event.new_title:
            host._publish(ChannelTitleChanged(channel_id=chat_id, new_title=event.new_title))
        if event.new_photo:
            await handle_chat_photo(host, chat_id, event.photo)
    except Exception:  # noqa: BLE001
        log.exception("ChatAction handling failed")


async def handle_chat_photo(host: Any, chat_id: int, photo: Any) -> None:
    """头像变化 — 下载到 session_dir/photos/{cid}.jpg;None = 头像被删。"""
    if photo is None:
        host._photo_cache.pop(chat_id, None)
        host._publish(ChannelPhotoChanged(channel_id=chat_id, local_path=None))
        return
    pid = getattr(photo, "id", None)
    if pid is not None and host._photo_cache.get(chat_id) == pid:
        return
    photos_dir = host._settings.session_dir / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    path = photos_dir / f"{chat_id}.jpg"
    try:
        assert host._client is not None
        await host._client.download_media(photo, file=str(path))
        if pid is not None:
            host._photo_cache[chat_id] = pid
        host._publish(ChannelPhotoChanged(channel_id=chat_id, local_path=str(path)))
    except Exception:  # noqa: BLE001
        log.debug("chat photo download failed for %s", chat_id, exc_info=True)


async def on_views(host: Any, update: types.UpdateChannelMessageViews) -> None:
    try:
        marked = utils.get_peer_id(types.PeerChannel(update.channel_id))
        host._publish(
            MessageInteractionsChanged(
                channel_id=marked,
                telegram_msg_id=int(update.id),
                views=int(update.views),
                reactions=None,
            )
        )
    except Exception:  # noqa: BLE001
        log.exception("UpdateChannelMessageViews handling failed")


async def on_reactions(host: Any, update: types.UpdateMessageReactions) -> None:
    try:
        if update.peer is None:
            return
        marked = utils.get_peer_id(update.peer)
        host._publish(
            MessageInteractionsChanged(
                channel_id=marked,
                telegram_msg_id=int(update.msg_id),
                views=None,
                reactions=map_reactions(update.reactions),
            )
        )
    except Exception:  # noqa: BLE001
        log.exception("UpdateMessageReactions handling failed")


async def on_pinned_channel(host: Any, update: types.UpdatePinnedChannelMessages) -> None:
    try:
        marked = utils.get_peer_id(types.PeerChannel(update.channel_id))
        pinned = bool(update.pinned)
        for mid in update.messages or []:
            host._publish(
                MessagePinChanged(channel_id=marked, telegram_msg_id=int(mid), is_pinned=pinned)
            )
    except Exception:  # noqa: BLE001
        log.exception("UpdatePinnedChannelMessages handling failed")


async def on_pinned(host: Any, update: types.UpdatePinnedMessages) -> None:
    try:
        if update.peer is None:
            return
        marked = utils.get_peer_id(update.peer)
        pinned = bool(update.pinned)
        for mid in update.messages or []:
            host._publish(
                MessagePinChanged(channel_id=marked, telegram_msg_id=int(mid), is_pinned=pinned)
            )
    except Exception:  # noqa: BLE001
        log.exception("UpdatePinnedMessages handling failed")


async def on_channel_updated(host: Any, update: types.UpdateChannel) -> None:
    marked = utils.get_peer_id(types.PeerChannel(update.channel_id))
    await host._refresh_channel_metadata(marked)


async def on_chat_updated(host: Any, update: types.UpdateChat) -> None:
    marked = utils.get_peer_id(types.PeerChat(update.chat_id))
    await host._refresh_channel_metadata(marked)


async def refresh_channel_metadata(host: Any, marked: int) -> None:
    """UpdateChannel/UpdateChat 只带 id — 重取 entity 发 ChannelMetadataChanged。"""
    try:
        assert host._client is not None
        entity = await host._client.get_entity(marked)
        dto = host._entity_to_dto(entity)
        if dto is None:
            return
        host._publish(
            ChannelMetadataChanged(
                channel_id=dto.id,
                title=dto.title,
                username=dto.username,
                member_count=dto.member_count,
                is_verified=dto.is_verified,
                is_scam=dto.is_scam,
                is_fake=dto.is_fake,
                has_protected_content=dto.has_protected_content,
            )
        )
    except Exception:  # noqa: BLE001
        log.debug("channel metadata refresh failed for %s", marked, exc_info=True)
