"""errors.py 私有错误 helper 单元测试(v2.0.0 后端无关版)。

`_extract_error_detail(e)` 的链式 fallback:`.message` → `str(e)` → "未知错误"。
用 Telethon RPCError(带 .message)+ 普通 Exception 覆盖各分支。

原 TDLib 版还测 `_translate_boot_error`(TDLib boot 错误码翻译)—
该函数随 TDLib 后端删除,Telethon 无对应概念。
"""

from __future__ import annotations

import pytest
from telethon.errors import RPCError

from tgmonitor.core.telegram.errors import _extract_error_detail

# ---- 1. RPCError 风格 exception(带 .message) ----


def test_extracts_message_from_rpc_error() -> None:
    err = RPCError(request=None, message="PHONE_NUMBER_INVALID", code=400)
    assert _extract_error_detail(err) == "PHONE_NUMBER_INVALID"


def test_extracts_message_when_rpc_error_message_is_empty() -> None:
    """`.message` 是空串时,fallback str(e)。"""
    err = RPCError(request=None, message="", code=400)
    assert _extract_error_detail(err) == str(err)


# ---- 2. 普通 Exception 没 `.message` 字段 ----


def test_falls_back_to_str_for_plain_exception() -> None:
    """`getattr(e, "message", None)` 返 None → str(e)。"""
    err = ValueError("something went wrong")
    assert _extract_error_detail(err) == "something went wrong"


def test_falls_back_to_str_for_timeout_error() -> None:
    """TimeoutError 没 .message — 走 str(exc) 路径。但 TimeoutError() 默认
    str() 是空,所以走 "未知错误" fallback。"""
    err = TimeoutError()
    # TimeoutError() 自身 str() 是 "",验证 chain 真的走到 "未知错误"
    assert _extract_error_detail(err) == "未知错误"


# ---- 3. 极端 fallback:str(exc) 也是空 → "未知错误" ----


def test_falls_back_to_unknown_when_all_empty() -> None:
    """`.message` None + str(e) 空 → '未知错误'。"""

    class _Empty(Exception):
        def __str__(self) -> str:
            return ""

    err = _Empty()
    assert _extract_error_detail(err) == "未知错误"


# ---- 4. 实际表现:链式"getattr message or str" 路径对照 ----


@pytest.mark.parametrize(
    "exc_class,attr_value,expected",
    [
        # 有 .message 优先
        (RPCError, "MSG", "MSG"),
        # 没 .message → str(e)
        (ValueError, None, "boom"),
        # 极端 fallback
        (Exception, None, "未知错误"),
    ],
)
def test_extract_error_detail_truthiness_chain(
    exc_class: type,
    attr_value: str | None,
    expected: str,
) -> None:
    """`getattr(...) or str(...) or "未知错误"` 链式判断逐项验证。"""
    if exc_class is RPCError:
        exc = RPCError(request=None, message=attr_value)
    elif exc_class is ValueError:
        exc = ValueError("boom")
    else:
        # Exception 子类,无 .message,str() 也空
        class _Empty(Exception):
            def __str__(self) -> str:
                return ""

        exc = _Empty()
    assert _extract_error_detail(exc) == expected
