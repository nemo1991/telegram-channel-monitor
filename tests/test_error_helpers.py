"""tdlib_client.py 私有错误 helper 单元测试。

`_extract_error_detail(e)` 出现 3 处(call site):
  - `_check_authentication_code` / `_check_authentication_password`
  - `_translate_rate_limit` 解析 "FLOOD_WAIT_NNN" 字符串

测试纯函数,不需要 aiotdlib stub / Qt / event loop — 跑得最快。
"""
from __future__ import annotations

import pytest

from tgmonitor.core.telegram.tdlib_client import _extract_error_detail


# ---- 1. AioTDLibError 风格 exception ----

class _FakeAioTDLibError(Exception):
    """模拟 aiotdlib 0.27 AioTDLibError — 有 `.message` 字段。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(f"{{code={code}, message={message}}}")
        self.message = message
        self.code = code


def test_extracts_message_from_aiotdlib_error() -> None:
    err = _FakeAioTDLibError("PHONE_NUMBER_INVALID", code=400)
    assert _extract_error_detail(err) == "PHONE_NUMBER_INVALID"


def test_extracts_message_when_aio_tdlib_error_message_is_empty() -> None:
    """`.message` 是空串时,fallback str(e)。"""
    err = _FakeAioTDLibError(message="", code=400)
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

@pytest.mark.parametrize("exc_class,attr_value,expected", [
    # 有 .message 优先
    (_FakeAioTDLibError, "MSG", "MSG"),
    # 没 .message → str(e)
    (ValueError, None, "boom"),
    # 极端 fallback
    (Exception, None, "未知错误"),
])
def test_extract_error_detail_truthiness_chain(
    exc_class: type, attr_value: str | None, expected: str,
) -> None:
    """`getattr(...) or str(...) or "未知错误"` 链式判断逐项验证。"""
    if exc_class is _FakeAioTDLibError:
        exc = _FakeAioTDLibError(message=attr_value)
    elif exc_class is ValueError:
        exc = ValueError("boom")
    else:
        # Exception 子类,无 .message,str() 也空
        class _Empty(Exception):
            def __str__(self) -> str:
                return ""
        exc = _Empty()
    assert _extract_error_detail(exc) == expected
