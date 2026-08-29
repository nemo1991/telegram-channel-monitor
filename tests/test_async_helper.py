"""run_coro helper 单元测试 — src/tgmonitor/ui/_async.py。

不依赖 Qt / qasync — 跑 raw asyncio loop 在背景线程,验证:
  1. success path:on_success 拿到返回值
  2. fire-and-forget:无 callback 时不抛、log 无 noise
  3. 异常归一:log.exception + on_error 都被触发
  4. on_success / on_error 自身抛 → log.exception 兜底,不污染 future
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

import pytest

from tgmonitor.ui._async import run_coro


@pytest.fixture
def bg_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """真在跑的事件 loop,跑在 background 线程,跟 caller 跨线程调 run_coro。"""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


def _wait_callbacks(ms: int = 100) -> None:
    """让 run_coro 的 `add_done_callback` 在 event loop 跑完。"""
    time.sleep(ms / 1000)


# ---- 1. success path ----


def test_runs_coro_and_calls_on_success_with_result(
    bg_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def ok() -> str:
        return "hello"

    got: list[str] = []
    fut = run_coro(bg_loop, ok(), on_success=got.append, error_label="ok")
    assert fut.result(timeout=2) == "hello"
    _wait_callbacks()
    assert got == ["hello"]
    # 没有任何 log 噪音
    assert "ok failed" not in caplog.text


def test_returns_future_even_without_callbacks(
    bg_loop: asyncio.AbstractEventLoop,
) -> None:
    """fire-and-forget:同样返 Future 给 caller 取句柄。"""

    async def ok() -> int:
        return 42

    fut = run_coro(bg_loop, ok(), error_label="ok")
    assert fut.result(timeout=2) == 42


# ---- 2. exception path ----


def test_logs_exception_and_calls_on_error(
    bg_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail() -> None:
        raise ValueError("boom")

    errs: list[BaseException] = []
    with caplog.at_level(logging.ERROR, logger="tgmonitor.ui._async"):
        fut = run_coro(
            bg_loop,
            fail(),
            on_error=errs.append,
            error_label="fail-test",
        )
        # fut.result() 抛 ValueError(原始异常从 coroutine 透传)
        with pytest.raises(ValueError, match="boom"):
            fut.result(timeout=1)
    _wait_callbacks()
    assert len(errs) == 1
    assert isinstance(errs[0], ValueError)
    assert str(errs[0]) == "boom"
    assert "fail-test failed" in caplog.text


def test_on_error_swallows_its_own_exception(
    bg_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """on_error 内部抛 → log.exception 兜底,不污染 future / 不往上抛。

    注:on_error 跑在 bg_loop 线程,我们先 wait 一下让它 log,再 caplog 检查。
    """

    async def fail() -> None:
        raise RuntimeError("orig")

    called: list[BaseException] = []

    def on_e(e: BaseException) -> None:
        called.append(e)
        raise RuntimeError("user-callback-crash")

    with caplog.at_level(logging.ERROR, logger="tgmonitor.ui._async"):
        fut = run_coro(
            bg_loop,
            fail(),
            on_error=on_e,
            error_label="fail2",
        )
        # fut.result() 拿的是"原" RuntimeError(orig),跟 on_error 内的 raise 无关
        with pytest.raises(RuntimeError, match="orig"):
            fut.result(timeout=1)
    _wait_callbacks()
    assert len(called) == 1
    # on_error 内部 raise 被 log.exception 兜底
    assert "on_error callback for fail2 raised" in caplog.text


def test_on_success_swallows_its_own_exception(
    bg_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def ok() -> str:
        return "result"

    def bad_success(r: str) -> None:
        raise RuntimeError("user-success-crash")

    with caplog.at_level(logging.ERROR, logger="tgmonitor.ui._async"):
        fut = run_coro(
            bg_loop,
            ok(),
            on_success=bad_success,
            error_label="ok2",
        )
        # success 跑过,on_success 抛了,但 fut.result() 仍是 success result
        assert fut.result(timeout=2) == "result"
    _wait_callbacks()
    assert "on_success callback for ok2 raised" in caplog.text


# ---- 3. 验证 typing 契约 ----


@pytest.mark.parametrize(
    "value,expected",
    [
        (42, int),
        ("str", str),
        (None, type(None)),
        ((1, 2, 3), tuple),
    ],
)
def test_on_success_value_type_passes_through(
    bg_loop: asyncio.AbstractEventLoop,
    value: Any,
    expected: Any,
) -> None:
    """TypeVar T 透传:coro 返 int / str / None / tuple 都没问题。"""

    async def coro() -> Any:
        return value

    seen: list[Any] = []

    def grab(r: Any) -> None:
        seen.append(r)

    run_coro(bg_loop, coro(), on_success=grab, error_label="t")
    _wait_callbacks(50)
    assert seen == [value]
    assert isinstance(seen[0], expected)
