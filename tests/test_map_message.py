"""`_map_message` 单元测试 — 覆盖全部 8 个媒体类 + 高频 6 个 service 类。

为什么不用 `make_*` 工厂放进 conftest.py:
- conftest.py 现有 `make_message / make_photo` 是**纯 DTO** 工厂,不调 `_map_message`。
- 本测试的输入是 aiotdlib pydantic 对象的 SimpleNamespace 模拟,
  `make_*` 工厂是**构造带正确 `__name__` 的类 + content 字段**,只服务于本测试,
  放别处没意义。

设计:
- `_c(ctype, **kwargs)` 用 `type(ctype, (SimpleNamespace,), {})` 造一个类名等于
  ctype 的类 — 这样 `type(content).__name__` 走我们 dispatch 字典的对应 key。
- 调用 `_map_message(simple_msg)` 直接验证返回的 `MessageDTO`。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from tgmonitor.core.dto import MediaType
from tgmonitor.core.telegram.tdlib_client import _map_message

# ---- helpers ----


def _c(ctype: str, **kwargs: Any) -> Any:
    """造一个类名是 ctype 的 content 对象(SimpleNamespace 模拟 aiotdlib pydantic 字段访问)。"""
    cls = type(ctype, (SimpleNamespace,), {})
    return cls(**kwargs)


def _msg(ctype: str, **content_kwargs: Any) -> SimpleNamespace:
    """造一个完整 Message SimpleNamespace — id=1, chat_id=100, date=0, 全部 content attrs。"""
    return SimpleNamespace(
        id=1,
        chat_id=100,
        date=0,
        views=0,
        forwards=0,
        edit_date=0,
        author_signature=None,
        content=_c(ctype, **content_kwargs),
    )


# ============================================================
# 8 个媒体类
# ============================================================


def test_message_text():
    msg = _msg("MessageText", text=SimpleNamespace(text="hi"), caption=None)
    dto = _map_message(msg)
    assert dto.text == "hi"
    assert dto.media == []


def test_message_photo_picks_biggest_size_by_area():
    """Photo 多个 sizes 时应取 width*height 最大的那个。"""
    file_big = SimpleNamespace(id=99, size=1234)
    file_small = SimpleNamespace(id=88, size=10)
    sizes = [
        SimpleNamespace(type_="m", photo=file_small, width=100, height=100, progressive_sizes=[]),
        SimpleNamespace(type_="x", photo=file_big, width=200, height=300, progressive_sizes=[]),
    ]
    ph = SimpleNamespace(sizes=sizes, minithumbnail=None, has_stickers=False)
    msg = _msg("MessagePhoto", photo=ph, caption=None)
    dto = _map_message(msg)
    assert len(dto.media) == 1
    assert dto.media[0].type == MediaType.PHOTO
    assert dto.media[0].width == 200
    assert dto.media[0].height == 300
    assert dto.media[0].file_size == 1234
    assert dto.media[0].telegram_file_id == "99"
    assert dto.text == ""  # caption 缺省


def test_message_photo_with_caption():
    cap = SimpleNamespace(text="look at this")
    ph = SimpleNamespace(
        sizes=[SimpleNamespace(type_="x", photo=SimpleNamespace(id=1, size=100), width=100, height=100, progressive_sizes=[])],
        minithumbnail=None, has_stickers=False,
    )
    msg = _msg("MessagePhoto", photo=ph, caption=cap)
    dto = _map_message(msg)
    assert dto.text == "look at this"
    assert dto.media[0].type == MediaType.PHOTO


def test_message_video():
    video = SimpleNamespace(
        width=1920, height=1080, duration=120, mime_type="video/mp4", file_name="v.mp4",
        video=SimpleNamespace(id=42, size=9999),
        thumbnail=SimpleNamespace(file=SimpleNamespace(id=33, size=200)),
    )
    msg = _msg("MessageVideo", video=video, caption=None)
    dto = _map_message(msg)
    assert len(dto.media) == 1
    m = dto.media[0]
    assert m.type == MediaType.VIDEO
    assert m.width == 1920 and m.height == 1080
    assert m.duration == 120
    assert m.mime_type == "video/mp4"
    assert m.telegram_file_id == "42"
    assert m.file_size == 9999
    assert m.thumb_key == "media/33.thumb"
    assert m.thumb_backend == "local"


def test_message_animation():
    animation = SimpleNamespace(
        width=320, height=240, duration=5, mime_type="video/mp4", file_name="anim.mp4",
        animation=SimpleNamespace(id=7, size=500),
        thumbnail=SimpleNamespace(file=SimpleNamespace(id=8, size=100)),
    )
    msg = _msg("MessageAnimation", animation=animation, caption=None)
    dto = _map_message(msg)
    assert dto.media[0].type == MediaType.ANIMATION
    assert dto.media[0].duration == 5
    assert dto.media[0].width == 320


def test_message_audio():
    audio = SimpleNamespace(
        duration=180, mime_type="audio/mp3", file_name="song.mp3",
        audio=SimpleNamespace(id=12, size=4096),
        album_cover_thumbnail=SimpleNamespace(file=SimpleNamespace(id=99, size=300)),
    )
    msg = _msg("MessageAudio", audio=audio, caption=SimpleNamespace(text="track name"))
    dto = _map_message(msg)
    m = dto.media[0]
    assert m.type == MediaType.AUDIO
    assert m.duration == 180
    assert m.mime_type == "audio/mp3"
    assert m.thumb_key == "media/99.thumb"
    assert dto.text == "track name"


def test_message_voice_note():
    voice_note = SimpleNamespace(
        duration=30, mime_type="audio/ogg",
        voice=SimpleNamespace(id=11, size=2048),
    )
    msg = _msg("MessageVoiceNote", voice_note=voice_note, caption=SimpleNamespace(text=""))
    dto = _map_message(msg)
    m = dto.media[0]
    assert m.type == MediaType.VOICE
    assert m.duration == 30
    assert m.mime_type == "audio/ogg"
    assert m.thumb_key is None  # voice 没缩略图


def test_message_video_note_square_dims():
    """VideoNote 用 length 字段同时表示 w=h(圆形)。"""
    vn = SimpleNamespace(
        length=240, duration=15,
        video=SimpleNamespace(id=10, size=2000),
        thumbnail=SimpleNamespace(file=SimpleNamespace(id=20, size=300)),
    )
    msg = _msg("MessageVideoNote", video_note=vn, is_viewed=False, is_secret=False)
    dto = _map_message(msg)
    m = dto.media[0]
    assert m.type == MediaType.VIDEO_NOTE
    assert m.width == 240
    assert m.height == 240
    assert m.duration == 15


def test_message_document_with_caption():
    doc = SimpleNamespace(
        mime_type="application/pdf", file_name="paper.pdf",
        document=SimpleNamespace(id=77, size=12345),
        thumbnail=SimpleNamespace(file=SimpleNamespace(id=88, size=400)),
    )
    msg = _msg("MessageDocument", document=doc, caption=SimpleNamespace(text="abstract"))
    dto = _map_message(msg)
    m = dto.media[0]
    assert m.type == MediaType.DOCUMENT
    assert m.mime_type == "application/pdf"
    assert m.file_name == "paper.pdf"
    assert m.thumb_key == "media/88.thumb"
    assert dto.text == "abstract"


def test_message_sticker_emoji():
    sticker = SimpleNamespace(
        width=512, height=512, emoji="😀",
        sticker=SimpleNamespace(id=11, size=4096),
        thumbnail=SimpleNamespace(file=SimpleNamespace(id=22, size=200)),
    )
    msg = _msg("MessageSticker", sticker=sticker, is_premium=False)
    dto = _map_message(msg)
    m = dto.media[0]
    assert m.type == MediaType.STICKER
    assert m.emoji == "😀"
    assert m.width == 512
    assert m.thumb_key == "media/22.thumb"
    assert dto.text == ""  # sticker 无 caption


# ============================================================
# 6 个高频 service 类
# ============================================================


def test_message_dice():
    msg = _msg("MessageDice", emoji="🎯", value=6)
    dto = _map_message(msg)
    assert dto.text == "🎲 🎯 6"
    assert dto.media == []


def test_message_location_normal():
    loc = SimpleNamespace(latitude=39.9042, longitude=116.4074)
    msg = _msg("MessageLocation", location=loc, live_period=0)
    assert _map_message(msg).text == "📍 39.9042, 116.4074"


def test_message_location_live_has_satellite_marker():
    loc = SimpleNamespace(latitude=1.0, longitude=2.0)
    msg = _msg("MessageLocation", location=loc, live_period=60)
    dto = _map_message(msg)
    assert "🛰️" in dto.text
    assert "1.0000, 2.0000" in dto.text


def test_message_contact():
    contact = SimpleNamespace(first_name="John", last_name="Doe", phone_number="8613800001234")
    msg = _msg("MessageContact", contact=contact)
    assert _map_message(msg).text == "📎 John Doe (+8613800001234)"


def test_message_poll_with_question():
    poll = SimpleNamespace(question=SimpleNamespace(text="Best color?"))
    msg = _msg("MessagePoll", poll=poll)
    assert _map_message(msg).text == "📊 Best color?"


def test_message_poll_no_question_fallback():
    msg = _msg("MessagePoll", poll=None)
    assert _map_message(msg).text == "📊 <poll>"


def test_message_unsupported_fallback():
    """类名不在两张表里 → [service: ClassName]。"""
    msg = _msg("MessageSomethingBrandNew")
    assert _map_message(msg).text == "[service: MessageSomethingBrandNew]"


def test_message_story():
    msg = _msg("MessageStory", story_sender_chat_id=12345, story_id=99, via_mention=False)
    assert _map_message(msg).text == "📖 转发的故事(频道 #12345)"


def test_message_gift():
    """带 text 的 Gift 应渲染两行。"""
    gift = SimpleNamespace(star_count=100)
    msg = _msg("MessageGift",
               gift=gift, sender_id=None, received_gift_id="r1",
               text=SimpleNamespace(text="happy bday"),
               prepaid_upgrade_star_count=0, sell_star_count=0,
               is_private=False, is_saved=False, can_be_upgraded=False,
               was_converted=False, was_upgraded=False, was_refunded=False,
               upgraded_received_gift_id=None)
    assert _map_message(msg).text == "🎁 100⭐ 礼物\n  happy bday"


# ============================================================
# 持久化 roundtrip — sticker emoji 写入 jsonl 后能读回
# ============================================================


async def test_sticker_emoji_roundtrip_jsonl(tmp_path):
    """emoji 字段必须经 jsonl_store 完整往返 — 不丢字段,不被改默认值。"""
    from tgmonitor.core.storage.jsonl_store import JsonlFileStore

    store = JsonlFileStore(tmp_path)
    await store.connect()
    # 构造一个 sticker 媒体消息
    from datetime import datetime

    from tgmonitor.core.dto import MediaDTO, MessageDTO

    msg = MessageDTO(
        id=0,
        channel_id=100,
        telegram_msg_id=42,
        date=datetime(2026, 7, 15, 12, 0, 0),
        text="",
        author=None,
        media=[MediaDTO(
            type=MediaType.STICKER, mime_type="image/webp",
            file_size=4096, width=512, height=512,
            thumb_key="media/sticker.webp.thumb", thumb_backend="local",
            emoji="😀",
        )],
    )
    await store.save_message(msg)
    # 验证文件里有 emoji
    import json
    raw = (tmp_path / "messages" / "100.jsonl").read_text(encoding="utf-8").strip()
    record = json.loads(raw)
    assert record["media"][0]["emoji"] == "😀"

    # 读回
    got = await store.get_message(100, 42)
    assert got is not None
    assert got.media[0].emoji == "😀"
    assert got.media[0].type == MediaType.STICKER


# ============================================================
# Extra edge cases
# ============================================================


def test_message_with_no_content_does_not_crash():
    """content 是 None 时 — 旧实现在生产遇到过,不应崩。"""
    msg = SimpleNamespace(
        id=1, chat_id=100, date=0, views=0, forwards=0, edit_date=0, author_signature=None,
        content=None,
    )
    dto = _map_message(msg)
    assert dto.text == ""
    assert dto.media == []


def test_message_video_caption_preserved():
    cap = SimpleNamespace(text="watch this")
    video = SimpleNamespace(
        width=100, height=100, duration=10, mime_type="video/mp4", file_name=None,
        video=SimpleNamespace(id=1, size=100), thumbnail=None,
    )
    msg = _msg("MessageVideo", video=video, caption=cap)
    dto = _map_message(msg)
    assert dto.text == "watch this"
