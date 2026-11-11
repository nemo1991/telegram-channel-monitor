"""Telethon 消息映射纯函数单测 — v2.0.0(离线,构造真 TL 对象,零网络)。

覆盖 `telethon_messages.py`:
- file-id 编解码(roundtrip / thumb / 非法 / 旧数字 id)
- `map_message`:纯文本 / 照片 / 各类 document / 链接预览(无媒体)/ service
- 媒体子类型 dispatch(sticker / voice / audio / video / video_note / animation)
- reactions(emoji / custom emoji / chosen)
- forward_origin(user / channel / chat / hidden)
- `pick_thumb_size`(~320px 挑选)
"""

from __future__ import annotations

from datetime import UTC, datetime

from telethon import types

from tgmonitor.core.dto import MediaType
from tgmonitor.core.telegram.telethon_messages import (
    encode_file_id,
    map_message,
    map_reactions,
    parse_file_id,
    pick_thumb_size,
)

_DATE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_CID = -1000000000123  # marked channel id


def _msg(**kw) -> types.Message:
    """最小 Message — peer_id / date 给默认,其余按 kw。"""
    base = dict(id=42, peer_id=types.PeerChannel(123), date=_DATE, message="")
    base.update(kw)
    return types.Message(**base)


def _photo(sizes: list) -> types.Photo:
    return types.Photo(
        id=999,
        access_hash=1,
        file_reference=b"\x00",
        date=_DATE,
        sizes=sizes,
        dc_id=2,
    )


def _doc(attributes: list, **kw) -> types.Document:
    base = dict(
        id=555,
        access_hash=1,
        file_reference=b"\x00",
        date=_DATE,
        mime_type="application/octet-stream",
        size=12345,
        dc_id=2,
        attributes=attributes,
    )
    base.update(kw)
    return types.Document(**base)


# ---- file-id 编解码 ----


def test_file_id_roundtrip() -> None:
    assert parse_file_id(encode_file_id(_CID, 42)) == (_CID, 42, False)
    assert parse_file_id(encode_file_id(_CID, 42, thumb=True)) == (_CID, 42, True)


def test_file_id_format() -> None:
    assert encode_file_id(-100123, 7) == "tl:-100123:7"
    assert encode_file_id(-100123, 7, thumb=True) == "tl:-100123:7:thumb"


def test_file_id_invalid_returns_none() -> None:
    assert parse_file_id("") is None
    assert parse_file_id("12345") is None  # 旧 TDLib 数字 file.id
    assert parse_file_id("garbage") is None
    assert parse_file_id("tl:abc:7") is None
    assert parse_file_id("tl:1:2:3") is None
    assert parse_file_id("tl:1:2:thumb:extra") is None


# ---- 纯文本 / 链接预览 ----


def test_plain_text() -> None:
    dto = map_message(_msg(message="hello world"))
    assert dto.channel_id == _CID
    assert dto.telegram_msg_id == 42
    assert dto.text == "hello world"
    assert dto.media == []
    assert dto.date == _DATE


def test_web_preview_produces_no_media() -> None:
    """MessageMediaWebPage(链接预览)不产生媒体 — 与 TDLib MessageText 同语义。"""
    media = types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))
    dto = map_message(_msg(message="see link", media=media))
    assert dto.media == []
    assert dto.text == "see link"


# ---- 照片 ----


def test_photo_mapping() -> None:
    photo = _photo(
        [
            types.PhotoSize(type="s", w=90, h=90, size=1000),
            types.PhotoSize(type="m", w=320, h=320, size=5000),
            types.PhotoSize(type="x", w=800, h=800, size=20000),
        ]
    )
    dto = map_message(_msg(message="caption", media=types.MessageMediaPhoto(photo=photo)))
    assert len(dto.media) == 1
    m = dto.media[0]
    assert m.type == MediaType.PHOTO
    assert m.mime_type == "image/jpeg"
    assert m.width == 800 and m.height == 800
    assert m.file_size == 20000
    assert m.telegram_file_id == encode_file_id(_CID, 42)
    assert m.thumbnail_telegram_file_id == encode_file_id(_CID, 42, thumb=True)
    assert dto.text == "caption"


def test_photo_progressive_size() -> None:
    photo = _photo([types.PhotoSizeProgressive(type="x", w=640, h=480, sizes=[100, 9000])])
    dto = map_message(_msg(media=types.MessageMediaPhoto(photo=photo)))
    assert dto.media[0].file_size == 9000
    assert dto.media[0].width == 640


def test_photo_empty_not_media() -> None:
    dto = map_message(_msg(media=types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1))))
    assert dto.media == []


# ---- document 子类型 ----


def test_video_document() -> None:
    doc = _doc(
        [
            types.DocumentAttributeVideo(duration=12.5, w=1920, h=1080),
            types.DocumentAttributeFilename(file_name="clip.mp4"),
        ],
        mime_type="video/mp4",
        thumbs=[types.PhotoSize(type="m", w=320, h=180, size=4000)],
    )
    dto = map_message(_msg(media=types.MessageMediaDocument(document=doc)))
    m = dto.media[0]
    assert m.type == MediaType.VIDEO
    assert m.mime_type == "video/mp4"
    assert m.file_name == "clip.mp4"
    assert m.width == 1920 and m.height == 1080
    assert m.duration == 12.5
    assert m.file_size == 12345
    assert m.thumbnail_telegram_file_id == encode_file_id(_CID, 42, thumb=True)


def test_video_note_round_message() -> None:
    doc = _doc([types.DocumentAttributeVideo(duration=3.0, w=240, h=240, round_message=True)])
    dto = map_message(_msg(media=types.MessageMediaDocument(document=doc)))
    assert dto.media[0].type == MediaType.VIDEO_NOTE


def test_voice_vs_audio() -> None:
    voice = _doc([types.DocumentAttributeAudio(duration=5, voice=True)])
    audio = _doc([types.DocumentAttributeAudio(duration=200, voice=False, title="Song")])
    assert map_message(_msg(media=types.MessageMediaDocument(document=voice))).media[0].type == MediaType.VOICE
    assert map_message(_msg(media=types.MessageMediaDocument(document=audio))).media[0].type == MediaType.AUDIO


def test_sticker_emoji() -> None:
    doc = _doc(
        [types.DocumentAttributeSticker(alt="😀", stickerset=types.InputStickerSetEmpty())],
        mime_type="image/webp",
    )
    m = map_message(_msg(media=types.MessageMediaDocument(document=doc))).media[0]
    assert m.type == MediaType.STICKER
    assert m.emoji == "😀"


def test_animation() -> None:
    doc = _doc([types.DocumentAttributeAnimated()], mime_type="image/gif")
    assert map_message(_msg(media=types.MessageMediaDocument(document=doc))).media[0].type == MediaType.ANIMATION


def test_plain_document_no_thumb() -> None:
    doc = _doc([types.DocumentAttributeFilename(file_name="report.pdf")], mime_type="application/pdf")
    m = map_message(_msg(media=types.MessageMediaDocument(document=doc))).media[0]
    assert m.type == MediaType.DOCUMENT
    assert m.file_name == "report.pdf"
    assert m.thumbnail_telegram_file_id is None  # 无 thumbs


# ---- service / 特殊媒体 → 文本 ----


def test_service_action_text() -> None:
    msg = _msg(message="", media=None)
    msg.action = types.MessageActionChatEditTitle(title="新名字")
    dto = map_message(msg)
    assert "新名字" in dto.text
    assert dto.media == []


def test_contact_text() -> None:
    media = types.MessageMediaContact(
        phone_number="1234567", first_name="张", last_name="三", vcard="", user_id=0
    )
    dto = map_message(_msg(media=media))
    assert "张 三" in dto.text
    assert dto.media == []


def test_geo_text() -> None:
    media = types.MessageMediaGeo(geo=types.GeoPoint(long=116.4, lat=39.9, access_hash=0))
    dto = map_message(_msg(media=media))
    assert "39.9" in dto.text and "116.4" in dto.text


def test_dice_text() -> None:
    dto = map_message(_msg(media=types.MessageMediaDice(value=5, emoticon="🎲")))
    assert "🎲" in dto.text and "5" in dto.text


def test_poll_text() -> None:
    poll = types.Poll(
        id=1,
        question=types.TextWithEntities(text="午饭吃什么?", entities=[]),
        answers=[],
        hash=0,
    )
    dto = map_message(_msg(media=types.MessageMediaPoll(poll=poll, results=types.PollResults())))
    assert "午饭吃什么" in dto.text


# ---- reactions ----


def test_map_reactions_emoji_and_custom() -> None:
    reactions = types.MessageReactions(
        results=[
            types.ReactionCount(reaction=types.ReactionEmoji(emoticon="🔥"), count=3, chosen_order=0),
            types.ReactionCount(reaction=types.ReactionCustomEmoji(document_id=777), count=2),
        ]
    )
    out = map_reactions(reactions)
    assert len(out) == 2
    assert out[0].type == "emoji" and out[0].emoji == "🔥" and out[0].count == 3
    assert out[0].is_chosen is True
    assert out[1].type == "custom_emoji" and out[1].is_chosen is False


def test_map_reactions_none() -> None:
    assert map_reactions(None) == []


def test_message_reactions_attached() -> None:
    reactions = types.MessageReactions(
        results=[types.ReactionCount(reaction=types.ReactionEmoji(emoticon="👍"), count=1)]
    )
    dto = map_message(_msg(reactions=reactions))
    assert dto.reactions is not None and len(dto.reactions) == 1


def test_message_no_reactions_is_none() -> None:
    assert map_message(_msg()).reactions is None


# ---- forward_origin ----


def test_forward_origin_user() -> None:
    fwd = types.MessageFwdHeader(date=_DATE, from_id=types.PeerUser(888))
    dto = map_message(_msg(fwd_from=fwd))
    assert dto.forward_origin["@type"] == "messageOriginUser"
    assert dto.forward_origin["sender_user_id"] == 888
    assert dto.forward_origin["date"] == int(_DATE.timestamp())


def test_forward_origin_channel() -> None:
    fwd = types.MessageFwdHeader(
        date=_DATE,
        from_id=types.PeerChannel(123),
        channel_post=99,
        post_author="作者",
    )
    dto = map_message(_msg(fwd_from=fwd))
    assert dto.forward_origin["@type"] == "messageOriginChannel"
    assert dto.forward_origin["chat_id"] == _CID
    assert dto.forward_origin["message_id"] == 99
    assert dto.forward_origin["author_signature"] == "作者"


def test_forward_origin_hidden_user() -> None:
    fwd = types.MessageFwdHeader(date=_DATE, from_name="隐藏的人")
    dto = map_message(_msg(fwd_from=fwd))
    assert dto.forward_origin["@type"] == "messageOriginHiddenUser"
    assert dto.forward_origin["sender_user_name"] == "隐藏的人"


def test_forward_origin_none() -> None:
    assert map_message(_msg()).forward_origin is None


# ---- 杂项字段 ----


def test_reply_edit_pin_album() -> None:
    msg = _msg(
        reply_to=types.MessageReplyHeader(reply_to_msg_id=10),
        edit_date=_DATE,
        pinned=True,
        grouped_id=777,
        views=100,
        forwards=5,
        post_author="签名",
    )
    dto = map_message(msg)
    assert dto.reply_to_msg_id == 10
    assert dto.edited is True
    assert dto.is_pinned is True
    assert dto.media_album_id == 777
    assert dto.views == 100
    assert dto.forwards == 5
    assert dto.author == "签名"


def test_zero_views_normalized_to_none() -> None:
    dto = map_message(_msg(views=0, forwards=0))
    assert dto.views is None
    assert dto.forwards is None


# ---- pick_thumb_size ----


def test_pick_thumb_size_near_320() -> None:
    photo = _photo(
        [
            types.PhotoSize(type="s", w=90, h=90, size=1000),
            types.PhotoSize(type="m", w=320, h=320, size=5000),
            types.PhotoSize(type="x", w=800, h=800, size=20000),
        ]
    )
    picked = pick_thumb_size(photo)
    assert picked is not None and picked.w == 320


def test_pick_thumb_size_excludes_stripped() -> None:
    photo = _photo([types.PhotoStrippedSize(type="i", bytes=b"\x00\x01")])
    assert pick_thumb_size(photo) is None


def test_pick_thumb_size_empty() -> None:
    assert pick_thumb_size(_photo([])) is None