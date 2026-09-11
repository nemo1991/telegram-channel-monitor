"""2026-09-11 v1.7.4:`format_reactions_short` helper 单元测试。

覆盖:
- None / 空 list → 空串
- count=0 跳过
- 基础 emoji 顺序
- max_show 截断 + `+N`
- 自投标记 `[]` 切换
- 自投标记 LIVE 行禁用(常见用法)
"""

from __future__ import annotations

from tgmonitor.core.dto import ReactionDTO
from tgmonitor.ui.widgets._reaction_format import format_reactions_short


def test_format_returns_empty_for_none() -> None:
    assert format_reactions_short(None) == ""


def test_format_returns_empty_for_empty_list() -> None:
    assert format_reactions_short([]) == ""


def test_format_basic_order() -> None:
    rs = [
        ReactionDTO(emoji="🔥", count=5),
        ReactionDTO(emoji="👍", count=3),
    ]
    assert format_reactions_short(rs) == "🔥 5  👍 3"


def test_format_skips_zero_count() -> None:
    """TDLib 偶尔推送 count=0 占位 — 必须跳过,避免显示「😀 0」噪音。"""
    rs = [
        ReactionDTO(emoji="🔥", count=5),
        ReactionDTO(emoji="👍", count=0),
        ReactionDTO(emoji="❤️", count=2),
    ]
    assert format_reactions_short(rs) == "🔥 5  ❤️ 2"


def test_format_marks_chosen_with_brackets_by_default() -> None:
    """默认 mark_chosen=True — 自投的 emoji 用 `[]` 包。"""
    rs = [
        ReactionDTO(emoji="🔥", count=5, is_chosen=True),
        ReactionDTO(emoji="👍", count=3, is_chosen=False),
    ]
    assert format_reactions_short(rs) == "[🔥 5]  👍 3"


def test_format_marks_chosen_disabled_for_live_row() -> None:
    """LIVE 行 mark_chosen=False — 紧凑显示,不标 `[]`。"""
    rs = [
        ReactionDTO(emoji="🔥", count=5, is_chosen=True),
        ReactionDTO(emoji="👍", count=3, is_chosen=True),
    ]
    assert format_reactions_short(rs, mark_chosen=False) == "🔥 5  👍 3"


def test_format_max_show_truncates_with_plus_n() -> None:
    """max_show=2 + 5 个 emoji → 只显前 2 + `+3`。"""
    rs = [
        ReactionDTO(emoji="🔥", count=5),
        ReactionDTO(emoji="👍", count=3),
        ReactionDTO(emoji="❤️", count=2),
        ReactionDTO(emoji="🎉", count=1),
        ReactionDTO(emoji="🤔", count=1),
    ]
    assert format_reactions_short(rs, max_show=2) == "🔥 5  👍 3  +3"


def test_format_max_show_zero_count_skipped_no_plus_n() -> None:
    """count=0 的 emoji 在循环开头被 skip,不算进 `+N`。"""
    rs = [
        ReactionDTO(emoji="🔥", count=5),
        ReactionDTO(emoji="👍", count=3),
        ReactionDTO(emoji="zero", count=0),  # 跳过,不计入 `+N`
    ]
    # max_show=2,前 2 个非零计数都被显;第 3 个 count=0 被跳过 → 无 `+N`
    assert format_reactions_short(rs, max_show=2) == "🔥 5  👍 3"


def test_format_max_show_all_zero_count_yields_no_plus_n() -> None:
    """全 count=0 → 全部跳过,空串,无 `+N`。"""
    rs = [
        ReactionDTO(emoji="a", count=0),
        ReactionDTO(emoji="b", count=0),
    ]
    assert format_reactions_short(rs, max_show=4) == ""
