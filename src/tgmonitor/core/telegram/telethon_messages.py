# mypy: disable-error-code="misc,assignment"
"""Telethon Message → MessageDTO 映射 + 媒体 / service 派发表。

v2.0.0 新增,对应 TDLib 时代的 `tdlib_messages.py`。定位:**pure functions,
零 state**。`map_message` 是入口,被 `TelethonTelegramClient` 的实时更新
handler 和 `iter_chat_history` 调用。

# file-id 编码(与 TDLib 的本质差异)

TDLib 给每个文件一个全局 `file.id`(跨消息去重、session 内稳定)。
Telethon 没有等价物(`File.id` 是 deprecated 的 bot-API pack,不可靠),
所以 v2 用**纯消息定位符**:

    tl:{chat_id}:{msg_id}         — 主媒体
    tl:{chat_id}:{msg_id}:thumb   — 缩略图

`chat_id` 是 marked id(`utils.get_peer_id`:channel = -100…,basic group
= -…,与 TDLib 的 chat_id 约定一致,DB 里的频道 id 无需迁移)。
下载 = `get_messages` 重取消息 → `download_media`(Telethon 内部会在下载
中途自动刷新过期的 file_reference,无 TDLib 的 file_reference 失效问题)。

代价:跨消息同文件不去重(TDLib file.id 可以);同消息重复同步仍去重
(MediaDownloader 的 skip-if-stored 按 file_id 查,定位符稳定)。
Telethon 一条消息最多 1 个媒体(相册 = 多条独立消息共享 grouped_id),
所以不需要 media_idx。

# 媒体 dispatch

直接 isinstance `msg.media`(不用 `msg.photo` / `msg.document` 便捷属性 —
它们会把**链接预览**的 photo/document 也算进来,TDLib 的 MessageText +
link preview 不产生媒体,这里保持同语义)。Document 子类型按 attributes
判:Sticker / Audio(voice) / Video(round_message) / Animated / 其它。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from telethon import types, utils
from telethon.tl.custom.file import File

from tgmonitor.core.dto import (
    MediaDTO,
    MediaType,
    MessageDTO,
    ReactionDTO,
)

log = logging.getLogger(__name__)

# ---- file-id 编解码 ----

_FILE_ID_PREFIX = "tl:"


def encode_file_id(chat_id: int, msg_id: int, *, thumb: bool = False) -> str:
    """消息定位符 → file-id 字符串(见模块 docstring 的编码约定)。"""
    base = f"{_FILE_ID_PREFIX}{chat_id}:{msg_id}"
    return f"{base}:thumb" if thumb else base


def parse_file_id(file_id: str) -> tuple[int, int, bool] | None:
    """file-id → `(chat_id, msg_id, is_thumb)`;非本编码 / 格式错 → None。

    旧 TDLib 时代的纯数字 file.id 在这里返 None — v2 不兼容旧 session 的
    媒体重下(那些 file.id 本来就只在旧 TDLib session 内有效)。
    """
    if not file_id or not file_id.startswith(_FILE_ID_PREFIX):
        return None
    parts = file_id[len(_FILE_ID_PREFIX) :].split(":")
    if len(parts) == 2:
        thumb = False
    elif len(parts) == 3 and parts[2] == "thumb":
        thumb = True
    else:
        return None
    try:
        return int(parts[0]), int(parts[1]), thumb
    except ValueError:
        return None


# ---- 通用小工具 ----


def _to_int_or_none(v: Any) -> int | None:
    """nullable int 归一:None / 0 / '' → None;可解析 → int;不可解析 → None。

    与 TDLib 版同语义(views / forwards 等 0 值按"未推送"处理)。
    """
    if v is None or v == 0 or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _unix_ts(dt: datetime | None) -> int | None:
    """datetime → unix 秒(forward_origin.date 与 TDLib 的 int 时间戳对齐)。"""
    if dt is None:
        return None
    return int(dt.timestamp())


def _pick_biggest_photo_size(photo: types.Photo) -> types.TypePhotoSize | None:
    """Photo.sizes 按 width*height 取最大的(跳过无尺寸的 stripped/path)。"""
    sizes = [
        s
        for s in (photo.sizes or [])
        if getattr(s, "w", 0) and getattr(s, "h", 0)
    ]
    if not sizes:
        return None
    return max(sizes, key=lambda s: s.w * s.h)


def pick_thumb_size(photo: types.Photo) -> types.TypePhotoSize | None:
    """给缩略图下载挑一个 ~320px 的 PhotoSize(TDLib thumbnail 的量级)。

    排除 stripped(模糊占位)/ path(SVG 轮廓)/ video(mp4)尺寸;
    没有候选时返 None(调用方回退 thumb=0)。
    """
    candidates = [
        s
        for s in (photo.sizes or [])
        if isinstance(s, (types.PhotoSize, types.PhotoSizeProgressive))
        and s.w
        and s.h
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda s: abs(max(s.w, s.h) - 320))


# ---- 媒体映射 ----


def _map_photo(media: types.MessageMediaPhoto, chat_id: int, msg_id: int) -> MediaDTO | None:
    photo = media.photo
    if not isinstance(photo, types.Photo):
        return None
    biggest = _pick_biggest_photo_size(photo)
    file_size = None
    if isinstance(biggest, types.PhotoSize):
        file_size = biggest.size
    elif isinstance(biggest, types.PhotoSizeProgressive):
        file_size = max(biggest.sizes, default=None)
    return MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_size=file_size,
        width=getattr(biggest, "w", None),
        height=getattr(biggest, "h", None),
        telegram_file_id=encode_file_id(chat_id, msg_id),
        # 照片总有更小的尺寸可当缩略图
        thumbnail_telegram_file_id=encode_file_id(chat_id, msg_id, thumb=True),
    )


def _document_media_type(doc: types.Document) -> tuple[MediaType, str | None]:
    """Document.attributes → (MediaType, sticker emoji)。"""
    for attr in doc.attributes:
        if isinstance(attr, types.DocumentAttributeSticker):
            return MediaType.STICKER, attr.alt
    for attr in doc.attributes:
        if isinstance(attr, types.DocumentAttributeAudio):
            return (MediaType.VOICE if attr.voice else MediaType.AUDIO), None
    for attr in doc.attributes:
        if isinstance(attr, types.DocumentAttributeVideo):
            return (
                MediaType.VIDEO_NOTE if attr.round_message else MediaType.VIDEO
            ), None
    for attr in doc.attributes:
        if isinstance(attr, types.DocumentAttributeAnimated):
            return MediaType.ANIMATION, None
    return MediaType.DOCUMENT, None


def _map_document(
    media: types.MessageMediaDocument, chat_id: int, msg_id: int
) -> MediaDTO | None:
    doc = media.document
    if not isinstance(doc, types.Document):
        return None
    media_type, emoji = _document_media_type(doc)
    f = File(doc)
    has_thumb = bool(doc.thumbs)
    return MediaDTO(
        type=media_type,
        mime_type=f.mime_type,
        file_name=f.name,
        file_size=doc.size,
        width=f.width,
        height=f.height,
        duration=f.duration,
        telegram_file_id=encode_file_id(chat_id, msg_id),
        thumbnail_telegram_file_id=(
            encode_file_id(chat_id, msg_id, thumb=True) if has_thumb else None
        ),
        emoji=emoji,
    )


# ---- service / 特殊媒体 → 文本 ----


def _contact_text(media: types.MessageMediaContact) -> str:
    name = f"{media.first_name or ''} {media.last_name or ''}".strip()
    phone = media.phone_number or ""
    return f"📎 {name} (+{phone})" if name else f"📎 (+{phone})"


def _geo_text(media: types.MessageMediaGeo | types.MessageMediaGeoLive) -> str:
    geo = media.geo
    if not isinstance(geo, types.GeoPoint):
        return "📍"
    suffix = " 🛰️" if isinstance(media, types.MessageMediaGeoLive) else ""
    return f"📍 {geo.lat:.4f}, {geo.long:.4f}{suffix}"


def _venue_text(media: types.MessageMediaVenue) -> str:
    title = media.title or ""
    addr = media.address or ""
    return f"📍 {title} — {addr}" if addr else f"📍 {title}"


def _poll_text(media: types.MessageMediaPoll) -> str:
    question = getattr(media.poll, "question", None)
    text = getattr(question, "text", None)
    return f"📊 {text}" if text else "📊 <poll>"


def _dice_text(media: types.MessageMediaDice) -> str:
    return f"🎲 {media.emoticon} {media.value}"


def _game_text(media: types.MessageMediaGame) -> str:
    return f"🎮 {getattr(media.game, 'title', '') or ''}"


def _invoice_text(media: types.MessageMediaInvoice) -> str:
    return f"🧾 {media.title or ''}"


# MessageAction* → 人类可读文本(对齐 TDLib _SERVICE_HANDLERS 的覆盖量级)
_ACTION_TEXT: dict[str, Any] = {
    "MessageActionChatCreate": lambda a: "🎉 群组已创建",
    "MessageActionChatEditTitle": lambda a: f"✏️ 群名改为「{a.title}」",
    "MessageActionChatEditPhoto": lambda a: "🖼 群头像已更新",
    "MessageActionChatDeletePhoto": lambda a: "🖼 群头像已删除",
    "MessageActionChatAddUser": lambda a: f"👥 添加了 {len(a.users or [])} 位成员",
    "MessageActionChatDeleteUser": lambda a: "👤 一位成员离开/被移除",
    "MessageActionChatJoinedByLink": lambda a: "🔗 通过邀请链接加入",
    "MessageActionChannelCreate": lambda a: "🎉 频道已创建",
    "MessageActionChatMigrateTo": lambda a: "➡️ 群组已升级为超级群",
    "MessageActionChannelMigrateFrom": lambda a: "⬅️ 由普通群升级而来",
    "MessageActionPinMessage": lambda a: "📌 置顶了一条消息",
    "MessageActionGameScore": lambda a: f"🎮 游戏得分 {a.score}",
    "MessageActionPaymentSentMe": lambda a: "💰 收到一笔付款",
    "MessageActionPaymentSent": lambda a: "💰 完成一笔付款",
    "MessageActionPhoneCall": lambda a: f"📞 通话 {a.duration or 0}s",
    "MessageActionScreenshotTaken": lambda a: "📸 截屏",
    "MessageActionCustomAction": lambda a: a.message or "",
    "MessageActionBotAllowed": lambda a: "🤖 已允许 bot 发消息",
    "MessageActionContactSignUp": lambda a: "📱 联系人注册了 Telegram",
    "MessageActionGeoProximityReached": lambda a: "📍 到达接近提醒范围",
    "MessageActionGroupCall": lambda a: f"📹 视频通话 {a.duration or 0}s",
    "MessageActionInviteToGroupCall": lambda a: (
        f"📹 邀请 {len(a.users or [])} 人加入视频通话"
    ),
    "MessageActionSetMessagesTTL": lambda a: "⏱ 设置了消息自动删除",
    "MessageActionGiftPremium": lambda a: "🎁 赠送了 Telegram Premium",
    "MessageActionTopicCreate": lambda a: f"🏷 创建话题「{a.title}」",
    "MessageActionTopicEdit": lambda a: "🏷 编辑了话题",
    "MessageActionSetChatWallPaper": lambda a: "🖼 设置了聊天壁纸",
    "MessageActionGiveawayLaunch": lambda a: "🎁 抽奖开始",
    "MessageActionGiveawayResults": lambda a: (
        f"🎁 抽奖结束 · {a.winners_count or 0} 名获奖者"
    ),
    "MessageActionBoostApply": lambda a: f"🚀 群组被 boost {a.boosts or 0} 次",
    "MessageActionSetChatTheme": lambda a: "🎨 设置了聊天主题",
    "MessageActionRequestedPeer": lambda a: "👤 分享了联系人",
    "MessageActionWebViewDataSentMe": lambda a: "🌐 收到 WebView 数据",
    "MessageActionSecureValuesSentMe": lambda a: "🔐 收到 Passport 数据",
    "MessageActionSuggestProfilePhoto": lambda a: "🖼 建议了头像",
}


def _action_text(action: Any) -> str:
    handler = _ACTION_TEXT.get(type(action).__name__)
    if handler is not None:
        try:
            return handler(action)
        except Exception:  # noqa: BLE001
            log.exception("action text mapping failed: %s", type(action).__name__)
    return f"[service: {type(action).__name__}]"


def _extract_media_and_text(msg: Any, chat_id: int, msg_id: int) -> tuple[list[MediaDTO], str]:
    """msg.media → (媒体列表, 文本)。文本 = 媒体 caption / 纯文本 / service 描述。"""
    # service 消息(MessageService.action)— 改名 / 置顶 / 加成员等,无媒体。
    action = getattr(msg, "action", None)
    if action is not None:
        return [], _action_text(action)
    media = getattr(msg, "media", None)
    caption = getattr(msg, "message", None) or ""
    if media is None:
        return [], caption
    if isinstance(media, types.MessageMediaPhoto):
        dto = _map_photo(media, chat_id, msg_id)
        return ([dto] if dto else []), caption
    if isinstance(media, types.MessageMediaDocument):
        dto = _map_document(media, chat_id, msg_id)
        return ([dto] if dto else []), caption
    # 链接预览不产生媒体(TDLib MessageText + link preview 同语义)
    if isinstance(media, types.MessageMediaWebPage):
        return [], caption
    if isinstance(media, types.MessageMediaContact):
        return [], _contact_text(media)
    if isinstance(media, (types.MessageMediaGeo, types.MessageMediaGeoLive)):
        return [], _geo_text(media)
    if isinstance(media, types.MessageMediaVenue):
        return [], _venue_text(media)
    if isinstance(media, types.MessageMediaPoll):
        return [], _poll_text(media)
    if isinstance(media, types.MessageMediaDice):
        return [], _dice_text(media)
    if isinstance(media, types.MessageMediaGame):
        return [], _game_text(media)
    if isinstance(media, types.MessageMediaInvoice):
        return [], _invoice_text(media)
    if isinstance(media, types.MessageMediaUnsupported):
        return [], "❓ 不支持的消息"
    if isinstance(media, types.MessageMediaEmpty):
        return [], caption
    # 其它(MessageMediaGiveaway / Story / PaidMedia 等)— 类名兜底
    return [], f"[media: {type(media).__name__}]"


# ---- forward_origin ----


def _map_forward_origin(fwd: types.MessageFwdHeader | None) -> dict[str, Any] | None:
    """MessageFwdHeader → TDLib messageOrigin* 风格的扁平 dict(存储/导出对齐)。

    TDLib 版只展开 4 种 origin;Telethon 的 fwd_from 按 from_id 的 Peer 类型
    对应到同样的 4 种,from_name-only(隐藏转发者)→ messageOriginHiddenUser。
    """
    if fwd is None:
        return None
    date = _unix_ts(fwd.date)
    from_id = fwd.from_id
    if isinstance(from_id, types.PeerUser):
        out: dict[str, Any] = {"@type": "messageOriginUser", "sender_user_id": from_id.user_id}
    elif isinstance(from_id, types.PeerChannel):
        out = {
            "@type": "messageOriginChannel",
            "chat_id": utils.get_peer_id(from_id),
            "message_id": fwd.channel_post,
            "author_signature": fwd.post_author,
        }
    elif isinstance(from_id, types.PeerChat):
        out = {
            "@type": "messageOriginChat",
            "sender_chat_id": utils.get_peer_id(from_id),
            "author_signature": fwd.post_author,
        }
    elif fwd.from_name:
        out = {"@type": "messageOriginHiddenUser", "sender_user_name": fwd.from_name}
    else:
        return None
    if date is not None:
        out["date"] = date
    # 与 TDLib 版一致:None 值字段不落 dict
    return {k: v for k, v in out.items() if v is not None}


# ---- reactions ----


def _map_reaction_count(rc: types.ReactionCount) -> ReactionDTO | None:
    """ReactionCount → ReactionDTO;无法识别的 reaction type 返 None 过滤。"""
    r = rc.reaction
    if isinstance(r, types.ReactionEmoji):
        return ReactionDTO(
            type="emoji",
            emoji=r.emoticon,
            count=rc.count,
            is_chosen=rc.chosen_order is not None,
        )
    if isinstance(r, types.ReactionCustomEmoji):
        return ReactionDTO(
            type="custom_emoji",
            emoji=f"🖼 {r.document_id}",  # 占位符,与 TDLib 版一致
            count=rc.count,
            is_chosen=rc.chosen_order is not None,
        )
    return None


def map_reactions(reactions: types.MessageReactions | None) -> list[ReactionDTO]:
    """MessageReactions → list[ReactionDTO]。

    None → 空 list;空 results 也是合法"全部撤回"信号。
    """
    if reactions is None:
        return []
    out: list[ReactionDTO] = []
    for rc in reactions.results or []:
        dto = _map_reaction_count(rc)
        if dto is not None:
            out.append(dto)
    return out


# ---- 入口 ----


def map_message(msg: Any) -> MessageDTO:
    """Telethon Message(custom 或 raw)→ MessageDTO。

    chat_id 用 `utils.get_peer_id(msg.peer_id)` 取 marked id — 与 TDLib 的
    chat_id 约定一致(channel = -100…,basic group = -…)。
    """
    chat_id = utils.get_peer_id(msg.peer_id)
    msg_id = msg.id
    media_list, text = _extract_media_and_text(msg, chat_id, msg_id)
    date = getattr(msg, "date", None)
    reply_to = getattr(msg, "reply_to", None)
    reactions = getattr(msg, "reactions", None)
    return MessageDTO(
        id=msg_id,
        channel_id=chat_id,
        telegram_msg_id=msg_id,
        author=getattr(msg, "post_author", None),
        date=date if isinstance(date, datetime) else datetime.now(UTC),
        text=text,
        views=_to_int_or_none(getattr(msg, "views", None)),
        forwards=_to_int_or_none(getattr(msg, "forwards", None)),
        reply_to_msg_id=_to_int_or_none(getattr(reply_to, "reply_to_msg_id", None)),
        edited=getattr(msg, "edit_date", None) is not None,
        media=media_list,
        forward_origin=_map_forward_origin(getattr(msg, "fwd_from", None)),
        via_bot_user_id=_to_int_or_none(getattr(msg, "via_bot_id", None)),
        media_album_id=_to_int_or_none(getattr(msg, "grouped_id", None)),
        is_pinned=bool(getattr(msg, "pinned", False)),
        reactions=map_reactions(reactions) if reactions is not None else None,
    )