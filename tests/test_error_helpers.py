"""tdlib_client.py 私有错误 helper 单元测试。

`_extract_error_detail(e)` 出现 3 处(call site):
  - `_check_authentication_code` / `_check_authentication_password`
  - `_translate_rate_limit` 解析 "FLOOD_WAIT_NNN" 字符串

`_translate_boot_error(seen_codes)` 在 `start()` 超时时把 TDLib 报的
error code 集合翻成人话,401 / 429 / 其他 / 空 各分支都要测试。

测试纯函数,不需要 TDLib stub / Qt / event loop — 跑得最快。
"""

from __future__ import annotations

import collections

import pytest
from tdlib_json import TdlibError

from tgmonitor.core.telegram.tdlib_errors import _extract_error_detail
from tgmonitor.core.telegram.tdlib_proxy import _translate_boot_error

# ---- 1. TdlibError 风格 exception ----


def test_extracts_message_from_tdlib_error() -> None:
    err = TdlibError(code=400, message="PHONE_NUMBER_INVALID")
    assert _extract_error_detail(err) == "PHONE_NUMBER_INVALID"


def test_extracts_message_when_tdlib_error_message_is_empty() -> None:
    """`.message` 是空串时,fallback str(e)。"""
    err = TdlibError(message="", code=400)
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
        (TdlibError, "MSG", "MSG"),
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
    if exc_class is TdlibError:
        exc = TdlibError(message=attr_value)
    elif exc_class is ValueError:
        exc = ValueError("boom")
    else:
        # Exception 子类,无 .message,str() 也空
        class _Empty(Exception):
            def __str__(self) -> str:
                return ""

        exc = _Empty()
    assert _extract_error_detail(exc) == expected


# ============================================================
# _translate_boot_error — start() 超时返回 detail 的翻译
# ============================================================


def _codes(*xs: int) -> collections.deque[int]:
    """把 int 列表装进 _seen_error_codes 真实使用的 deque 类型。"""
    return collections.deque(xs)


def test_translate_boot_error_returns_generic_when_no_codes() -> None:
    """空 deque → generic「TDLib 启动超时」 — 意味着桥接静默死(代理/DC 不可达)。"""
    assert _translate_boot_error(_codes()) == "TDLib 启动超时(可能代理不可达或 DC 不通)"


def test_translate_boot_error_401_wins_over_other_codes() -> None:
    """401 是 special — AppService 据此外层 rotate key;即使 deque 同时有别的
    code,401 优先级最高(任何 set 都走 encryption-key 分支)。"""
    detail = _translate_boot_error(_codes(500, 401, 429))
    assert "encryption key 不匹配" in detail
    assert "401" in detail


def test_translate_boot_error_429_when_no_401() -> None:
    """无 401 但有 429 → 限流分支。"""
    detail = _translate_boot_error(_codes(429, 500))
    assert "限流" in detail
    assert "429" in detail


def test_translate_boot_error_generic_dc_with_other_codes() -> None:
    """无 401 / 429,只有别的 code → DC 握手失败,带 codes 列表给用户排错。"""
    detail = _translate_boot_error(_codes(500, 502))
    assert "DC 握手失败" in detail
    assert "500" in detail
    assert "502" in detail


def test_translate_boot_error_handles_deque_slice_view() -> None:
    """类型契约:`seen_codes` 是 `collections.deque`(实际类型),不只接 list /
    set。deque 的 membership + iteration 都通过。"""
    dq = _codes(-500)
    assert "DC 握手失败" in _translate_boot_error(dq)
    dq.append(401)
    assert "encryption key 不匹配" in _translate_boot_error(dq)


def test_translate_boot_error_lock_msg_preferred() -> None:
    """code=400 + 「Can't lock file ... already in use」msg → 提示另一个实例,
    而不是误导性的「DC 握手失败」(2026-08-13 线上:旧实例占用 td.binlog)。"""
    msg = (
        "Can't lock file \"/Users/me/Library/Application Support/tgmonitor"
        '/session/tdlib/database/td.binlog", because it is already in use; '
        "check for another program instance running"
    )
    detail = _translate_boot_error(_codes(400), msg)
    assert "另一个 tgmonitor 实例" in detail
    assert "DC 握手失败" not in detail


def test_translate_boot_error_plain_msg_keeps_dc_detail() -> None:
    """有 codes 但 msg 不含 lock 关键词 → 仍回落到 DC 握手失败。"""
    detail = _translate_boot_error(_codes(400), "Wrong parameters")
    assert "DC 握手失败" in detail
