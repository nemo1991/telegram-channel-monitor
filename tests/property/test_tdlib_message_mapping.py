"""2026-09-17 PR 6:`_map_message` synthetic TDLib payload mapping test。

PR 6 plan 测点 6:用 fake TDLib message object → `_map_message(msg)` →
验证 MessageDTO 关键字段正确(text / media / pin / date / reply_to)。

测法:
- 构造 fake TDLib message objects(用 SimpleNamespace / 工厂)
- 跑 `_map_message`
- 断言关键字段 round-trip

重点:`_map_message` 走 `getattr` 防御式编程,所以 object 不必真继承
tdlib_json 类型 — 任意 SimpleNamespace / Mock 都能用。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# TDLib messageContent union 中常见 @type 字符串
CONTENT_TYPES = [
    "messageText",
    "messagePhoto",
    "messageVideo",
    "messageSticker",
    "messageDocument",
    "messageAudio",
    "messageVoiceNote",
    "messageAnimation",
    "messageVideoNote",
    "messageLocation",
    "messageContact",
    "messagePoll",
    # service 类 — 走 _SERVICE_HANDLERS
    "messageChatAddMembers",
    "messageChatDeleteMember",
    "messagePinMessage",
    "messageScreenshotTaken",
]


def _make_msg(
    *,
    msg_id: int = 1,
    chat_id: int = 100,
    date: int = 0,
    author_sig: str | None = None,
    views: int | None = None,
    forwards: int | None = None,
    edit_date: int = 0,
    is_pinned: bool = False,
    reply_to: int | None = None,
    forward_origin: Any = None,
    via_bot_user_id: int | None = None,
    media_album_id: str | None = None,
    content: Any = None,
) -> Any:
    """最小 TDLib-like Message obj。

    `_map_message` 用 `getattr` 取字段,所以 SimpleNamespace 够用。
    """
    return SimpleNamespace(
        id=msg_id,
        chat_id=chat_id,
        date=date,
        author_signature=author_sig,
        views=views,
        forwards=forwards,
        edit_date=edit_date,
        is_pinned=is_pinned,
        reply_to_message_id=reply_to,
        forward_origin=forward_origin,
        via_bot_user_id=via_bot_user_id,
        media_album_id=media_album_id,
        content=content,
    )


# ========== property:核心字段映射 ==========


@given(
    msg_id=st.integers(min_value=0, max_value=10**12),
    chat_id=st.integers(min_value=1, max_value=10**12),
    views=st.one_of(st.none(), st.integers(min_value=0, max_value=10**9)),
    is_pinned=st.booleans(),
)
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_map_message_scalar_fields(
    msg_id: int, chat_id: int, views: int | None, is_pinned: bool
) -> None:
    """`_map_message` 标量字段映射:id / chat_id→channel_id / views / is_pinned。"""
    from tgmonitor.core.telegram.tdlib_messages import _map_message

    msg = _make_msg(
        msg_id=msg_id,
        chat_id=chat_id,
        views=views,
        is_pinned=is_pinned,
    )
    dto = _map_message(msg)
    # id / telegram_msg_id 来自同一个 msg.id 字段
    assert dto.id == msg_id
    assert dto.telegram_msg_id == msg_id
    assert dto.channel_id == chat_id
    # 2026-09-23 v1.8.x:`_to_int_or_none` 把 int 0 也归 None(TDLib int53
    # sentinel);None 透传;正整数原样。
    assert dto.views is None if views in (None, 0) else dto.views == views
    assert dto.is_pinned == is_pinned


@given(
    author_sig=st.one_of(st.none(), st.text(min_size=1, max_size=64)),
    is_pinned=st.booleans(),
    edit_date=st.integers(min_value=0, max_value=10**9),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_map_message_author_and_edited(
    author_sig: str | None, is_pinned: bool, edit_date: int
) -> None:
    """`_map_message` author / edited 字段。"""
    from tgmonitor.core.telegram.tdlib_messages import _map_message

    msg = _make_msg(
        author_sig=author_sig,
        edit_date=edit_date,
        is_pinned=is_pinned,
    )
    dto = _map_message(msg)
    assert dto.author == author_sig
    # edited=True iff edit_date > 0
    assert dto.edited is (edit_date > 0)


# ========== dispatch:content.type_name → 走对应 handler ==============


@pytest.mark.parametrize("ctype", CONTENT_TYPES)
def test_map_message_content_type_dispatch(ctype: str) -> None:
    """任意 TDLib content @type → `_map_message` 不抛。

    各 content type 走 `_MEDIA_HANDLERS` 或 `_SERVICE_HANDLERS` 或
    `_fallback_service`,只要 dispatch 表覆盖到就不抛。
    """
    from tgmonitor.core.telegram.tdlib_messages import _map_message

    # 用 SimpleNamespace 模拟 content 对象(type_name 决定 dispatch)
    content = SimpleNamespace(type_name=ctype, text=SimpleNamespace(text=f"hello-{ctype}"))
    msg = _make_msg(content=content)

    dto = _map_message(msg)
    # 不抛 + 返回有效 DTO
    assert dto is not None
    assert dto.channel_id == 100


def test_map_message_unknown_content_type_uses_fallback() -> None:
    """未知 @type → 走 `_fallback_service` 不抛。"""
    from tgmonitor.core.telegram.tdlib_messages import _map_message

    content = SimpleNamespace(
        type_name="messageBrandNewTypeInFutureTDLib",
        text=SimpleNamespace(text="future"),
    )
    msg = _make_msg(content=content)
    dto = _map_message(msg)
    assert dto is not None
    # fallback 应返空 str 或包含 type name
    assert isinstance(dto.text, str)


def test_map_message_no_content() -> None:
    """`content=None` → 不抛,text=""。"""
    from tgmonitor.core.telegram.tdlib_messages import _map_message

    msg = _make_msg(content=None)
    dto = _map_message(msg)
    assert dto.text == ""
    assert dto.media == []


def test_map_message_empty_message_no_attrs() -> None:
    """完全空的 message(SimpleNamespace())→ getattr 兜底全默认值,不抛。"""
    from tgmonitor.core.telegram.tdlib_messages import _map_message

    msg = SimpleNamespace()  # 完全没有属性
    dto = _map_message(msg)
    assert dto.id == 0
    assert dto.channel_id == 0
    assert dto.telegram_msg_id == 0
    assert dto.text == ""
    assert dto.media == []
    assert dto.edited is False
    assert dto.is_pinned is False


# ========== 日期字段 ==============


def test_map_message_date_zero_uses_now() -> None:
    """`date=0` → fallback 到 `datetime.now(UTC)`。"""
    from datetime import UTC, datetime

    from tgmonitor.core.telegram.tdlib_messages import _map_message

    msg = _make_msg(date=0)
    before = datetime.now(UTC)
    dto = _map_message(msg)
    after = datetime.now(UTC)
    # 应在 [before, after] 区间(允许微小时钟漂移)
    assert before <= dto.date <= after


def test_map_message_date_unix_epoch() -> None:
    """`date=1700000000` → 2023-11-14 (UTC 准确时间)。"""
    from datetime import UTC, datetime

    from tgmonitor.core.telegram.tdlib_messages import _map_message

    msg = _make_msg(date=1700000000)
    dto = _map_message(msg)
    expected = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert dto.date == expected


# ========== forward_origin 边界 ==============


def test_map_message_forward_origin_none() -> None:
    """`forward_origin=None` → dto.forward_origin is None。"""
    from tgmonitor.core.telegram.tdlib_messages import _map_message

    msg = _make_msg(forward_origin=None)
    dto = _map_message(msg)
    assert dto.forward_origin is None


def test_map_message_reply_to_zero_is_none() -> None:
    """`reply_to_message_id=0` → `dto.reply_to_msg_id is None`(TDLib 用 0 表示无 reply)。"""
    from tgmonitor.core.telegram.tdlib_messages import _map_message

    msg = _make_msg(reply_to=0)
    dto = _map_message(msg)
    assert dto.reply_to_msg_id is None


def test_map_message_reply_to_nonzero_preserved() -> None:
    """`reply_to_message_id=42` → `dto.reply_to_msg_id == 42`。"""
    from tgmonitor.core.telegram.tdlib_messages import _map_message

    msg = _make_msg(reply_to=42)
    dto = _map_message(msg)
    assert dto.reply_to_msg_id == 42


# ========== 2026-09-23 v1.8.x:TDLib `'0'` str sentinel 防御 ==========


@given(
    album_id=st.one_of(
        st.none(),
        st.integers(min_value=0, max_value=10**12),
        st.sampled_from(["0", "1", "42", "", "999"]),
    ),
    views=st.one_of(
        st.none(),
        st.integers(min_value=0, max_value=10**9),
        st.sampled_from(["0", "42", ""]),
    ),
    reply_to=st.one_of(
        st.none(),
        st.integers(min_value=0, max_value=10**12),
        st.sampled_from(["0", "1", ""]),
    ),
    via_bot=st.one_of(
        st.none(),
        st.integers(min_value=0, max_value=10**12),
        st.sampled_from(["0", "1", ""]),
    ),
)
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_map_message_int53_fields_any_input_safe(
    album_id: Any, views: Any, reply_to: Any, via_bot: Any
) -> None:
    """回归:`_to_int_or_none` 在 int / str / None / sentinel 上都安全。

    关键不变量:输出永远是 `int | None` — 永远不会有 `str` 类型漏到 DTO
    (那是 v1.8.1 现网 bug)。
    """
    from tgmonitor.core.telegram.tdlib_messages import _map_message

    msg = _make_msg(
        views=views,
        reply_to=reply_to,
        via_bot_user_id=via_bot,
        media_album_id=album_id,
    )
    dto = _map_message(msg)
    for field, raw in (
        ("views", views),
        ("reply_to_msg_id", reply_to),
        ("via_bot_user_id", via_bot),
        ("media_album_id", album_id),
    ):
        out = getattr(dto, field)
        assert out is None or isinstance(out, int), (
            f"{field}: input {raw!r} (type {type(raw).__name__}) → "
            f"output {out!r} (type {type(out).__name__}); must be int | None"
        )
        # 0 sentinel 一致归 None
        if raw in (None, 0, "0", ""):
            assert out is None, f"{field}: input {raw!r} must normalize to None"
