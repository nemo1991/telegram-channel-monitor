"""`_to_int_or_none` 集中兜底 helper 单元测试 — 2026-09-23 v1.8.x。

背景:TDLib Python 绑定对「absent」int53 字段(`media_album_id` /
`via_bot_user_id` / `reply_to_message_id` / `views` / `forwards` /
`File.size`)在某些 JSON 解码路径下返回字符串 `'0'`(不是 int `0`)。
原写法 `getattr(...) or None` 不触发(`'0'` truthy),字符串直传
dataclass DTO,最终 asyncpg 拒收 → `DataError: invalid input for
query argument $N: '0' ('str' object cannot be interpreted as an integer)`。
`_to_int_or_none` 是单一转换点。

哨兵清单(None / 0 / '0' / '')+ 正常值 + 边界(可解析字符串 / 不可解析字符串 / bool)。
"""

from __future__ import annotations

import pytest

from tgmonitor.core.telegram.tdlib_messages import _to_int_or_none


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # sentinels → None
        (None, None),
        (0, None),
        ("0", None),
        ("", None),
        # 正常 int / 字符串 int
        (1, 1),
        (-1, -1),
        (42, 42),
        ("1", 1),
        ("-42", -42),
        ("100", 100),
        # 大整数(int53 上限附近)
        (2**53, 2**53),
        (2**63 - 1, 2**63 - 1),
        # 浮点 / 数值字符串
        (1.5, 1),  # int(1.5) = 1
        # 注:int("1.5") 抛 ValueError → None(纯 int 字符串才合法)
        # 不可解析
        ("abc", None),
        ("0x10", None),  # int("0x10") 默认 base=10 失败 → None
        ([], None),  # TypeError → None
        ({}, None),
        (object(), None),
        # bool — Python 里 `True == 1` / `False == 0`,被 0 哨兵抓(False → None)。
        # 这是正确的:False 在 DTO 上下文里就是「absent」语义,等同 None。
        (True, 1),
        (False, None),
    ],
)
def test_to_int_or_none(value, expected) -> None:
    """`_to_int_or_none` 在所有典型输入上返回正确 sentinel / int / None。"""
    assert _to_int_or_none(value) == expected


def test_to_int_or_none_zero_string_does_not_leak_to_dto() -> None:
    """回归断言:`'0'` 字符串绝不返回 `'0'` str(会污染 dataclass DTO
    并最终让 asyncpg 抛 `DataError: 'str' object cannot be interpreted as
    an integer`)。
    """
    result = _to_int_or_none("0")
    assert result is None, (
        f"_to_int_or_none('0') must return None, got {result!r} "
        f"(type {type(result).__name__}) — would leak str into MessageDTO"
    )


def test_to_int_or_none_int_zero_does_not_leak_to_dto() -> None:
    """int 0 也归一为 None(TDLib int53 用 0 表示 absent — 与现有
    `getattr(...) or None` 语义一致)。
    """
    assert _to_int_or_none(0) is None
