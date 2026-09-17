"""2026-09-17 PR 6:`_format_meta_icons` emoji 顺序不变量 property test。

PR 6 plan 测点 2:任意 MessageDTO → `_format_meta_icons` 输出的 emoji 段
必须按 ★ → 🏷 → 📝 → 📌 → reactions 固定顺序。

测法:
1. 构造多种 DTO 组合(空、单字段、全字段、tags 多/少、reactions 等)
2. 跑 `_format_meta_icons`
3. 断言 emoji 段出现顺序符合预期

bug 锁定机制:每个 property 用 `@example` 锁几条边界 case;hypothesis 跑 200
random case 验证不变量。

注:`_format_meta_icons` 在 `src/tgmonitor/ui/widgets/message_view.py` 是
private 函数,但 plan 要求测。import 走 `from ... import _format_meta_icons`。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from tgmonitor.core.dto import (
    MessageDTO,
    ReactionDTO,
)
from tgmonitor.ui.widgets.message_view import _format_meta_icons

# ========== helpers ==========


def _make_msg(
    *,
    is_favorite: bool = False,
    tags: list[str] | None = None,
    notes: str = "",
    is_pinned: bool = False,
    reactions: list[ReactionDTO] | None = None,
) -> MessageDTO:
    """最小构造 MessageDTO 用于 emoji invariant 测。"""
    return MessageDTO(
        id=1,
        channel_id=100,
        telegram_msg_id=1,
        date=datetime(2026, 9, 17, tzinfo=UTC),
        text="x",
        is_favorite=is_favorite,
        tags=list(tags) if tags else [],
        notes=notes,
        is_pinned=is_pinned,
        reactions=list(reactions) if reactions else None,
    )


def _first_index_of(s: str, needle: str) -> int:
    """返 needle 在 s 里第一次出现的 idx;不存在返 -1。"""
    return s.find(needle)


# ========== property:固定顺序 ==========


@pytest.mark.parametrize(
    "msg,expected_segments",
    [
        # 0. 全空 → 空串
        (_make_msg(), []),
        # 1. 只有 favorite
        (_make_msg(is_favorite=True), ["★"]),
        # 2. 只有 tag
        (_make_msg(tags=["tech"]), ["🏷"]),
        # 3. 只有 notes
        (_make_msg(notes="hello"), ["📝"]),
        # 4. 只有 pin
        (_make_msg(is_pinned=True), ["📌"]),
        # 5. ★ → 🏷
        (_make_msg(is_favorite=True, tags=["t"]), ["★", "🏷"]),
        # 6. 🏷 → 📝
        (_make_msg(tags=["t"], notes="n"), ["🏷", "📝"]),
        # 7. 📝 → 📌
        (_make_msg(notes="n", is_pinned=True), ["📝", "📌"]),
        # 8. 全 4 段 + reactions
        (
            _make_msg(
                is_favorite=True,
                tags=["t"],
                notes="n",
                is_pinned=True,
                reactions=[ReactionDTO(emoji="🔥", count=5)],
            ),
            ["★", "🏷", "📝", "📌"],
        ),
    ],
)
def test_format_meta_icons_segment_order(msg: MessageDTO, expected_segments: list[str]) -> None:
    """`_format_meta_icons` 输出:每段存在则按 ★→🏷→📝→📌→reactions 顺序出现。

    用 `find` 取每段首次出现 idx,断言 idx 单调递增。
    """
    out = _format_meta_icons(msg)
    indices = [_first_index_of(out, seg) for seg in expected_segments]
    # 任何一段缺失 → `find` 返 -1 → 报错
    for seg, idx in zip(expected_segments, indices, strict=False):
        assert idx >= 0, f"segment {seg!r} 缺失(输出 {out!r})"
    # 出现的 idx 单调递增(顺序固定)
    assert indices == sorted(indices), (
        f"emoji 段顺序错乱:indices={indices}, expected={sorted(indices)}, "
        f"output={out!r}"
    )


# ========== hypothesis property:不变量全开 ==========


@given(
    is_favorite=st.booleans(),
    tags=st.one_of(
        st.none(),
        st.lists(st.sampled_from(["t", "ai", "x"]), max_size=2),
    ),
    notes=st.one_of(st.none(), st.text(max_size=20)),
    is_pinned=st.booleans(),
)
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_format_meta_icons_invariant_order(is_favorite, tags, notes, is_pinned) -> None:
    """任意 4 维组合(fav/tags/notes/pinned)→ 出现段按 ★→🏷→📝→📌 排序。"""
    msg = _make_msg(
        is_favorite=is_favorite,
        tags=tags,
        notes=notes or "",
        is_pinned=is_pinned,
    )
    out = _format_meta_icons(msg)

    present_segments: list[str] = []
    if is_favorite:
        present_segments.append("★")
    if tags:
        present_segments.append("🏷")
    if notes:
        present_segments.append("📝")
    if is_pinned:
        present_segments.append("📌")

    indices = [_first_index_of(out, seg) for seg in present_segments]
    assert indices == sorted(indices), (
        f"emoji 段顺序错乱:present={present_segments}, "
        f"indices={indices}, output={out!r}"
    )


# ========== 边界 case:全空 / 单段 / reactions ==============


def test_format_meta_icons_empty() -> None:
    """无 metadata → 空串(不是空格 / None)。"""
    assert _format_meta_icons(_make_msg()) == ""


def test_format_meta_icons_only_favorite() -> None:
    """只有 fav → 单段 ★(无空格前缀/后缀)。"""
    assert _format_meta_icons(_make_msg(is_favorite=True)) == "★"


def test_format_meta_icons_reactions_appended_after_pin() -> None:
    """reactions 在 📌 之后(plan:reactions 是末段)。"""
    msg = _make_msg(
        is_pinned=True,
        reactions=[ReactionDTO(emoji="🔥", count=5)],
    )
    out = _format_meta_icons(msg)
    pin_idx = _first_index_of(out, "📌")
    rx_idx = _first_index_of(out, "🔥")
    assert pin_idx >= 0 and rx_idx >= 0
    assert rx_idx > pin_idx, f"reactions 应在 📌 之后,output={out!r}"


@example(notes="x" * 100)
@example(notes="x")
@example(notes="")
@example(notes="\x85")  # NEL — boundary
@given(notes=st.text(max_size=80))
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_format_meta_icons_notes_truncates_at_30(notes: str) -> None:
    """`notes` > 30 字符截断加 `…`;≤ 30 字符原样保留。"""
    msg = _make_msg(notes=notes)
    out = _format_meta_icons(msg)
    if not notes:
        assert "📝" not in out
        return  # type: ignore[return-value]
    # 有 notes → 📝 段必有
    assert "📝" in out
    if len(notes) > 30:
        # 截断:out 应包含 `…` 在 📝 之后
        marker_idx = _first_index_of(out, "…")
        assert marker_idx > 0, f"超 30 字符 notes 应有 `…` 截断,output={out!r}"
    else:
        # 无截断:不应有 `…`
        assert "…" not in out, f"≤ 30 字符不应有 `…`,output={out!r}"