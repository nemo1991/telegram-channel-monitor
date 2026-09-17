"""2026-09-17 PR 6:hypothesis strategies for DTO round-trip property tests。

集中所有 DTO 的 `from hypothesis import composite` 策略,供 `tests/property/`
下各 file 复用。每个 strategy 给合理边界(避免 assumption 过多)+ 不抛构造错
(空 list / None 字段都允许,对应 DTO 实际可空)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import strategies as st

from tgmonitor.core.dto import (
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    ReactionDTO,
)

# ============== 基础标量 ==============


@st.composite
def timestamps(draw) -> datetime:
    """UTC datetime,1970-2030 范围 — 覆盖历史 + 未来边界。"""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = timedelta(seconds=draw(st.integers(min_value=0, max_value=60 * 60 * 24 * 365 * 60)))
    return epoch + delta


channel_ids = st.integers(min_value=1, max_value=10**12 - 1)
telegram_msg_ids = st.integers(min_value=1, max_value=10**12 - 1)
db_pks = st.integers(min_value=1, max_value=10**12 - 1)


# ============== MediaDTO ==============


@st.composite
def media_dtos(draw) -> MediaDTO:
    """随机 MediaDTO,关键字段(extension / status / object_key)允许 None。

    不传 .id(没有该字段)。
    """
    return MediaDTO(
        type=draw(st.sampled_from(list(MediaType))),
        mime_type=draw(
            st.one_of(
                st.none(),
                st.sampled_from(["image/jpeg", "video/mp4", "audio/ogg", "image/png"]),
            )
        ),
        file_name=draw(st.one_of(st.none(), st.text(min_size=1, max_size=64))),
        file_size=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=10**10))),
        width=draw(st.one_of(st.none(), st.integers(min_value=1, max_value=10000))),
        height=draw(st.one_of(st.none(), st.integers(min_value=1, max_value=10000))),
        duration=draw(st.one_of(st.none(), st.integers(min_value=1, max_value=86400))),
        telegram_file_id=draw(
            st.one_of(st.none(), st.text(min_size=8, max_size=64).filter(lambda s: s.strip()))
        ),
        object_key=draw(st.one_of(st.none(), st.text(min_size=1, max_size=128))),
        object_backend=draw(st.one_of(st.none(), st.sampled_from(["local", "s3"]))),
        thumb_key=draw(st.one_of(st.none(), st.text(min_size=1, max_size=128))),
        thumb_backend=draw(st.one_of(st.none(), st.sampled_from(["local", "s3"]))),
        download_status=draw(st.sampled_from(list(MediaDownloadStatus))),
        download_error=draw(st.one_of(st.none(), st.text(max_size=128))),
        emoji=draw(
            st.one_of(
                st.none(),
                st.text(min_size=1, max_size=8).filter(
                    lambda s: any(ord(ch) > 127 for ch in s) or any(ch.isalnum() for ch in s)
                ),
            )
        ),
    )


# ============== ReactionDTO ==============


@st.composite
def reaction_dtos(draw) -> ReactionDTO:
    emoji_str = draw(
        st.text(min_size=1, max_size=8).filter(
            lambda s: any(ord(c) > 127 for c in s) or any(c.isalnum() for c in s)
        )
    )
    return ReactionDTO(
        type=draw(st.sampled_from(["emoji", "custom_emoji"])),
        emoji=emoji_str,
        count=draw(st.integers(min_value=1, max_value=10**6)),
        is_chosen=draw(st.booleans()),
    )


# ============== MessageDTO ==============


@st.composite
def message_dtos(draw) -> MessageDTO:
    """随机 MessageDTO,允许 text 为空、media list 0-3 条、reactions 0-3 条。

        字段对齐 `tgmonitor.core.dto.MessageDTO`(v1.4.0+ 字段集):
    - `forward_origin` / `via_bot_user_id` / `media_album_id` 用 simple dict
    - `edited` 是字段名(不是 is_edited)
    - `raw` 留 None(避免 hypothesis 造 dict 太大)
    """
    return MessageDTO(
        id=draw(db_pks),
        channel_id=draw(channel_ids),
        telegram_msg_id=draw(telegram_msg_ids),
        author=draw(st.one_of(st.none(), st.text(min_size=1, max_size=64))),
        date=draw(timestamps()),
        text=draw(st.text(min_size=0, max_size=512)),
        views=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=10**9))),
        forwards=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=10**6))),
        reply_to_msg_id=draw(st.one_of(st.none(), telegram_msg_ids)),
        edited=draw(st.booleans()),
        media=draw(st.lists(media_dtos(), min_size=0, max_size=3)),
        raw=None,
        forward_origin=draw(
            st.one_of(
                st.none(),
                st.fixed_dictionaries({"@type": st.just("messageOriginUser"), "id": db_pks}),
            )
        ),
        via_bot_user_id=draw(st.one_of(st.none(), telegram_msg_ids)),
        media_album_id=draw(st.one_of(st.none(), st.text(min_size=1, max_size=32))),
        is_pinned=draw(st.booleans()),
        reactions=draw(
            st.one_of(
                st.none(),
                st.lists(reaction_dtos(), min_size=0, max_size=3),
            )
        ),
        is_favorite=draw(st.booleans()),
        tags=draw(st.lists(st.text(min_size=1, max_size=32), min_size=0, max_size=5)),
        notes=draw(st.text(min_size=0, max_size=512)),
    )
