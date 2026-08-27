# mypy: disable-error-code="misc,assignment"
"""TDLib Message → MessageDTO 映射 + 媒体 / service 派发表。

模块拆分(2026-08-02):从 `tdlib_client.py` 抽出。

定位:**pure functions,零 state**。`_map_message` 是入口,被
`TdlibTelegramClient._on_new_message` 和 `iter_chat_history` 调用,
把 TDLib `Message` 对象转 `MessageDTO`。

# 派发表

两张 handler 表 + 一个 fallback:
  - `_MEDIA_HANDLERS` — 8 个携带媒体二进制的 Message* 类型,
    返回 `(list[MediaDTO], caption_text)`
  - `_SERVICE_HANDLERS` — 30+ 个 service 类,返回人类可读文本
  - `_fallback_service` — 不在两张表里的类 → `[service: ClassName]`

任何新类型只需在表里加一行,无需改 `_map_message` 本体。
dispatch 用 `content.type_name`(`tdlib_json.TDLibObject` 把 `@type`
首字母大写:"messagePhoto" → "MessagePhoto");对非 TDLibObject 输入
兜底回退到 `type(content).__name__`。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from tgmonitor.core.dto import (
    MediaDTO,
    MediaType,
    MessageDTO,
)

log = logging.getLogger(__name__)


def _extract_caption(content: Any) -> str:
    """`MessagePhoto.caption` / `MessageVideo.caption` 等的 FormattedText 提取。

    旧实现里 caption.text 可能是 None 或 str;不抛异常,空就空。
    """
    cap = getattr(content, "caption", None)
    if cap is None:
        return ""
    inner = getattr(cap, "text", None)
    return inner if isinstance(inner, str) else ""


def _formatted_text(content: Any, attr: str) -> str:
    """`content.<attr>` 是 FormattedText → 拿 inner `.text`;若已是 str 直接返回。

    `tdlib_json` 的 FormattedText 是 TDLibObject,`.text` 字段是 str;
    但万一传进来的就是 str(legacy / fake),也要兜底。
    """
    if content is None:
        return ""
    ft = getattr(content, attr, None)
    if ft is None:
        return ""
    if isinstance(ft, str):
        return ft
    inner = getattr(ft, "text", None)
    return inner if isinstance(inner, str) else ""


def _pick_biggest_photo_size(photo: Any) -> Any:
    """Photo.sizes 按 width*height 取最大的 PhotoSize。

    旧实现按 `.size` 字段取,TDLib 那个字段是 File.size 而不是 PhotoSize 自身,
    实际我们想要的是最大面积的图片 → 用 width*height 更准。
    """
    sizes = getattr(photo, "sizes", None) or []
    if not sizes:
        return None
    return max(
        sizes,
        key=lambda s: (getattr(s, "width", 0) or 0) * (getattr(s, "height", 0) or 0),
    )


def _file_id(file_obj: Any) -> str | None:
    """File.id → str;None → None。"""
    if file_obj is None:
        return None
    fid = getattr(file_obj, "id", None)
    return str(fid) if fid is not None else None


def _file_size(file_obj: Any) -> int | None:
    if file_obj is None:
        return None
    return getattr(file_obj, "size", None) or None


def _thumb_key_from(thumbnail: Any) -> tuple[str | None, str | None]:
    """Thumbnail.file.id → (thumb_key, thumb_backend);无 thumbnail → (None, None)。"""
    if thumbnail is None:
        return None, None
    f = getattr(thumbnail, "file", None)
    if f is None:
        return None, None
    fid = getattr(f, "id", None)
    if fid is None:
        return None, None
    return f"media/{fid}.thumb", "local"


# ---- 媒体 handler 工厂 ----
# 7 个非 Photo 非 Sticker 的媒体类(MessagePhoto / MessageSticker 各自特殊)
# 共用一个工厂:取 media_obj 上的 file_attr(File 对象)→ id/size/dims/duration/thumbnail。


def _build_media_handler(
    media_type: MediaType,
    *,
    media_obj: str,
    file_attr: str,
    mime_default: str | None = None,
    has_dims: bool = False,
    dims_square: bool = False,
    has_duration: bool = True,
    thumb_attr: str | None = "thumbnail",
):
    """返回一个 fn(content) -> (list[MediaDTO], caption_text) 闭包。

    - media_obj: content 下挂的媒体对象字段(如 "video" / "audio" / "voice_note")
    - file_attr: media_obj 下挂的 File 对象字段(如 "video" / "audio" / "voice")
    - mime_default: 媒体对象没给 mime_type 时兜底;None = 用对象自带的 mime_type
    - has_dims: 是否取 width/height
    - dims_square: VideoNote 用,length 同时表示 w=h
    - has_duration: 是否取 duration(秒);audio/voice/video/animation/video_note = True;document = False
    - thumb_attr: media_obj 下挂的 Thumbnail 字段名;None = 无缩略图(voice_note)
    """
    def _fn(content: Any) -> tuple[list[MediaDTO], str]:
        obj = getattr(content, media_obj, None)
        if obj is None:
            return ([], _extract_caption(content))
        file_obj = getattr(obj, file_attr, None)
        kwargs: dict = {
            "type": media_type,
            "mime_type": getattr(obj, "mime_type", None) or mime_default,
            "file_name": getattr(obj, "file_name", None),
            "telegram_file_id": _file_id(file_obj),
            "file_size": _file_size(file_obj),
        }
        if has_dims:
            if dims_square:
                length = getattr(obj, "length", None)
                kwargs["width"] = length
                kwargs["height"] = length
            else:
                kwargs["width"] = getattr(obj, "width", None)
                kwargs["height"] = getattr(obj, "height", None)
        if has_duration:
            kwargs["duration"] = getattr(obj, "duration", None)
        if thumb_attr:
            th = getattr(obj, thumb_attr, None)
            tk, tb = _thumb_key_from(th)
            kwargs["thumb_key"] = tk
            kwargs["thumb_backend"] = tb
        return ([MediaDTO(**kwargs)], _extract_caption(content))
    return _fn


def _handle_photo(content: Any) -> tuple[list[MediaDTO], str]:
    """Photo 特殊:从 sizes[] 里按面积取最大 PhotoSize。"""
    ph = getattr(content, "photo", None)
    if ph is None:
        return ([], _extract_caption(content))
    biggest = _pick_biggest_photo_size(ph)
    file_obj = getattr(biggest, "photo", None) if biggest is not None else None
    return (
        [MediaDTO(
            type=MediaType.PHOTO,
            mime_type="image/jpeg",
            file_size=_file_size(file_obj),
            width=getattr(biggest, "width", None) if biggest is not None else None,
            height=getattr(biggest, "height", None) if biggest is not None else None,
            telegram_file_id=_file_id(file_obj),
        )],
        _extract_caption(content),
    )


def _handle_sticker(content: Any) -> tuple[list[MediaDTO], str]:
    """Sticker 特殊:无 caption / duration / mime_type,有 emoji。"""
    st = getattr(content, "sticker", None)
    if st is None:
        return ([], "")
    file_obj = getattr(st, "sticker", None)
    th = getattr(st, "thumbnail", None)
    tk, tb = _thumb_key_from(th)
    return (
        [MediaDTO(
            type=MediaType.STICKER,
            file_size=_file_size(file_obj),
            width=getattr(st, "width", None),
            height=getattr(st, "height", None),
            telegram_file_id=_file_id(file_obj),
            thumb_key=tk,
            thumb_backend=tb,
            emoji=getattr(st, "emoji", None),
        )],
        "",
    )


_MEDIA_HANDLERS: dict[str, Any] = {
    "MessagePhoto": _handle_photo,
    "MessageVideo": _build_media_handler(
        MediaType.VIDEO,
        media_obj="video", file_attr="video",
        has_dims=True, thumb_attr="thumbnail",
    ),
    "MessageAnimation": _build_media_handler(
        MediaType.ANIMATION,
        media_obj="animation", file_attr="animation",
        has_dims=True, thumb_attr="thumbnail",
    ),
    "MessageAudio": _build_media_handler(
        MediaType.AUDIO,
        media_obj="audio", file_attr="audio",
        mime_default="audio/mpeg",
        has_dims=False, thumb_attr="album_cover_thumbnail",
    ),
    "MessageVoiceNote": _build_media_handler(
        MediaType.VOICE,
        media_obj="voice_note", file_attr="voice",
        mime_default="audio/ogg",
        has_dims=False, thumb_attr=None,  # voice 没缩略图
    ),
    "MessageVideoNote": _build_media_handler(
        MediaType.VIDEO_NOTE,
        media_obj="video_note", file_attr="video",
        has_dims=True, dims_square=True, thumb_attr="thumbnail",
    ),
    "MessageDocument": _build_media_handler(
        MediaType.DOCUMENT,
        media_obj="document", file_attr="document",
        has_dims=False, has_duration=False, thumb_attr="thumbnail",
    ),
    "MessageSticker": _handle_sticker,
}


# ---- Service handler(只产生 text)----


def _handle_dice(content: Any) -> str:
    return f"🎲 {getattr(content, 'emoji', '🎲')} {getattr(content, 'value', 0)}"


def _handle_location(content: Any) -> str:
    loc = getattr(content, "location", None)
    if loc is None:
        return ""
    lat = getattr(loc, "latitude", 0.0) or 0.0
    lon = getattr(loc, "longitude", 0.0) or 0.0
    suffix = " 🛰️" if getattr(content, "live_period", 0) else ""
    return f"📍 {lat:.4f}, {lon:.4f}{suffix}"


def _handle_venue(content: Any) -> str:
    venue = getattr(content, "venue", None)
    if venue is None:
        return ""
    title = getattr(venue, "title", "") or ""
    addr = getattr(venue, "address", "") or ""
    return f"📍 {title} — {addr}" if addr else f"📍 {title}"


def _handle_contact(content: Any) -> str:
    c = getattr(content, "contact", None)
    if c is None:
        return ""
    fn = (getattr(c, "first_name", "") or "").strip()
    ln = (getattr(c, "last_name", "") or "").strip()
    name = f"{fn} {ln}".strip()
    phone = getattr(c, "phone_number", "") or ""
    return f"📎 {name} (+{phone})" if name else f"📎 (+{phone})"


def _handle_poll(content: Any) -> str:
    p = getattr(content, "poll", None)
    if p is None:
        return "📊 <poll>"
    q = getattr(p, "question", None)
    return "📊 " + _formatted_text(q, "text") if q is not None else "📊 <poll>"


def _handle_call(content: Any) -> str:
    dur = getattr(content, "duration", 0) or 0
    is_video = bool(getattr(content, "is_video", False))
    prefix = "📹 视频通话" if is_video else "📞 通话"
    return f"{prefix} {dur}s"


def _handle_video_chat_scheduled(content: Any) -> str:
    start = getattr(content, "start_date", 0) or 0
    if not start:
        return "📅 视频通话已安排"
    return "📅 视频通话已安排 " + datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%d %H:%M UTC")


def _handle_gift(content: Any) -> str:
    g = getattr(content, "gift", None)
    stars = getattr(g, "star_count", 0) if g is not None else 0
    is_private = bool(getattr(content, "is_private", False))
    text = _formatted_text(content, "text")
    base = "🎁 私密礼物" if is_private else f"🎁 {stars}⭐ 礼物"
    return f"{base}\n  {text}" if text else base


def _handle_gifted_premium(content: Any) -> str:
    months = getattr(content, "month_count", 0) or 0
    text = _formatted_text(content, "text")
    base = f"⭐ Telegram Premium {months} 个月"
    return f"{base}\n  {text}" if text else base


def _handle_giveaway_created(content: Any) -> str:
    # MessageGiveawayCreated 直接有 star_count(无 nested Gift)
    stars = getattr(content, "star_count", 0) or 0
    return f"🎁 抽奖已创建 · {stars}⭐" if stars else "🎁 抽奖已创建"


def _handle_upgraded_gift(content: Any) -> str:
    g = getattr(content, "gift", None)
    title = getattr(g, "title", "") if g is not None else ""
    number = getattr(g, "number", 0) if g is not None else 0
    if number:
        return f"💎 #{number}: {title}" if title else f"💎 #{number}"
    return f"💎 {title}" if title else "💎 升级版礼物"


_SERVICE_HANDLERS: dict[str, Any] = {
    "MessageText": lambda c: _formatted_text(c, "text"),
    "MessageDice": _handle_dice,
    "MessageAnimatedEmoji": lambda c: f"✨ {getattr(c, 'emoji', '')}",
    "MessageLocation": _handle_location,
    "MessageVenue": _handle_venue,
    "MessageContact": _handle_contact,
    "MessagePoll": _handle_poll,
    "MessageCall": _handle_call,
    "MessageCustomServiceAction": lambda c: _formatted_text(c, "text"),
    "MessageVideoChatScheduled": _handle_video_chat_scheduled,
    "MessageVideoChatStarted": lambda c: "📹 视频通话已开始",
    "MessageVideoChatEnded": lambda c: (
        f"📹 视频通话已结束({getattr(c, 'duration', 0)}s)"
    ),
    "MessageInviteVideoChatParticipants": lambda c: (
        f"📹 邀请 {len(getattr(c, 'user_ids', []) or [])} 人加入视频通话"
    ),
    "MessageStory": lambda c: (
        f"📖 转发的故事(频道 #{getattr(c, 'story_sender_chat_id', 0)})"
    ),
    "MessageGame": lambda c: f"🎮 {_formatted_text(getattr(c, 'game', None), 'title')}",
    "MessageGift": _handle_gift,
    "MessageGiftedPremium": _handle_gifted_premium,
    "MessageGiftedStars": lambda c: (
        f"⭐ {getattr(getattr(c, 'gift', None), 'star_count', 0)} Stars"
    ),
    "MessageGiveaway": lambda c: (
        f"🎁 抽奖开始({getattr(c, 'winner_count', 0)} 名获奖者)"
    ),
    "MessageGiveawayCreated": _handle_giveaway_created,
    "MessageGiveawayCompleted": lambda c: (
        f"🎁 抽奖结束 · {getattr(c, 'winner_count', 0)} 名获奖者"
    ),
    "MessageGiveawayWinners": lambda c: (
        f"🏆 {getattr(c, 'winner_count', 0)} 名获奖者"
    ),
    "MessageGiveawayPrizeStars": lambda c: (
        f"⭐ {getattr(c, 'star_count', 0)} Stars 抽奖奖励"
    ),
    "MessageUpgradedGift": _handle_upgraded_gift,
    "MessageRefundedUpgradedGift": lambda c: "↩️ 礼物已退款",
    "MessagePremiumGiftCode": lambda c: (
        f"🎟️ Premium 兑换码: {getattr(c, 'month_count', 0)} 个月"
    ),
    "MessagePinMessage": lambda c: (
        f"📌 已置顶消息 #{getattr(c, 'message_id', 0)}"
    ),
    "MessageChatBoost": lambda c: (
        f"🚀 群组被 boost {getattr(c, 'boost_count', 0)} 次"
    ),
    "MessageUnsupported": lambda c: "❓ 不支持的消息",
    "MessageExpiredPhoto": lambda c: "🕯️ 自毁照片已过期",
    "MessageExpiredVideo": lambda c: "🕯️ 自毁视频已过期",
    "MessageExpiredVideoNote": lambda c: "🕯️ 自毁视频消息已过期",
    "MessageExpiredVoiceNote": lambda c: "🕯️ 自毁语音已过期",
}


def _fallback_service(content: Any) -> str:
    """不在两张表里的类 — 显示类名,后续可补。"""
    return f"[service: {getattr(content, 'type_name', None) or type(content).__name__}]"


def _map_message(msg: Any) -> MessageDTO:
    """TDLib Message → MessageDTO。

    `tdlib_json` 的 TDLibObject 把 messageContent union 的 `@type`
    (messageText / messagePhoto / ...) 暴露为 `type_name` property。
    dispatch 用 `content.type_name`,见顶部注释。
    """
    chat_id = getattr(msg, "chat_id", 0)
    content = getattr(msg, "content", None)
    media_list: list[MediaDTO] = []
    text_value: str = ""
    if content is not None:
        ctype_name = getattr(content, "type_name", None) or type(content).__name__
        media_handler = _MEDIA_HANDLERS.get(ctype_name)
        if media_handler is not None:
            media_list, text_value = media_handler(content)
        else:
            text_value = _SERVICE_HANDLERS.get(ctype_name, _fallback_service)(content)
    date_ts = getattr(msg, "date", 0)
    return MessageDTO(
        id=getattr(msg, "id", 0),
        channel_id=chat_id,
        telegram_msg_id=getattr(msg, "id", 0),
        author=getattr(msg, "author_signature", None),
        date=datetime.fromtimestamp(date_ts, UTC) if date_ts else datetime.now(UTC),
        text=text_value,
        views=getattr(msg, "views", None),
        forwards=getattr(msg, "forwards", None),
        edited=getattr(msg, "edit_date", 0) > 0,
        media=media_list,
        # 2026-08-27 v1.4.0 PR #9:补 TDLib Message 5 个 v1.3.0 丢弃的字段。
        reply_to_msg_id=getattr(msg, "reply_to_message_id", None) or None,
        forward_origin=_normalize_forward_origin(
            getattr(msg, "forward_origin", None),
        ),
        via_bot_user_id=getattr(msg, "via_bot_user_id", None) or None,
        media_album_id=getattr(msg, "media_album_id", None) or None,
        is_pinned=bool(getattr(msg, "is_pinned", False)),
    )


# 2026-08-27 v1.4.0 PR #9:把 TDLib messageOrigin* 对象扁平化。
# TDLib 经常加新 origin type,我们只展开常见 4 种,其余保留 type_name 不展。
_FORWARD_ORIGIN_TYPES = {
    "messageOriginUser", "messageOriginChannel",
    "messageOriginHiddenUser", "messageOriginChat",
}


def _normalize_forward_origin(fo: Any) -> dict[str, Any] | None:
    """把 TDLib messageOrigin* TDLibObject 扁平化为 dict,UI 渲染用。

    只展开常见 4 种;新 type 出现时降级保留 `@type` 不展,避免 leak 私有
    字段。返回 None 当 fo 为 None。
    """
    if fo is None:
        return None
    type_name = getattr(fo, "type_name", None) or type(fo).__name__
    if type_name not in _FORWARD_ORIGIN_TYPES:
        return {"@type": type_name}
    out: dict[str, Any] = {"@type": type_name}
    # 提取常见字段;tdlib_json 把字段暴露为属性。
    for attr in ("sender_user_id", "sender_chat_id", "author_signature",
                 "chat_id", "message_id", "date"):
        v = getattr(fo, attr, None)
        if v is not None:
            out[attr] = v
    return out