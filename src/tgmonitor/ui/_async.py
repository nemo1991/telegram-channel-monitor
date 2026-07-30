"""_async.py — UI 跨线程调 async coroutine 的统一样板。

`qasync` 把 Qt 主线程当 asyncio loop 跑,但有些路径(signal handler / 第三方
callback / 启动期)需要从别的线程或同步上下文把 coroutine 推上 loop —
`asyncio.run_coroutine_threadsafe` 是正解,但 10+ 处调用点都套
`try: f.result() except Exception: log.exception(...)` 的同一个 callback
模板(「只 log 异常」/「log + 业务」两种形态)。

抽 `run_coro(loop, coro, *, on_success, on_error, error_label)` 统一入口:
  - 异常归一:log.exception(error_label);如给 `on_error` 再调一下(给 UI 弹窗)
  - 成功路径:on_success(f.result())(None = fire-and-forget)
  - 返回 Future,留给 caller 取句柄(目前 0 caller 需要,但保留以便未来)

设计原则(对应 #6 `form_row.py` 的 helper 思路):
  - 抽「如何 fire + 异常保存」成本,不只是抽 boilerplate
  - 不抽 on_success 内部逻辑 — 业务差异太大(QMessageBox / setText / emit / ...),
    让 caller 写 lambda
  - 不引入新依赖,纯 stdlib + PySide6 qasync
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


def run_coro(
    loop: asyncio.AbstractEventLoop,
    coro: Coroutine[Any, Any, T],
    *,
    on_success: Callable[[T], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
    error_label: str = "async task",
) -> asyncio.Future[T]:
    """跨线程把 coroutine 推上 loop,完成后回调到 caller(通常 Qt 主线程)。

    使用模式:
      1. **fire-and-forget**(7 处现有):`run_coro(self.loop, _go())`
         — 异常只走 log,无业务侧 callback
      2. **log + 业务**(2 处):`run_coro(self.loop, coro,
            on_success=lambda r: self._render(r))`
      3. **log + UI 弹窗**(2 处):`run_coro(self.loop, coro,
            on_success=lambda r: ..., error_label="submit_code")`

    返回 Future(目前 0 caller 用,保留接口稳定性)。

    异常语义:
      - 调用 `f.result()` 抛:`log.exception(error_label)` + 调 `on_error(exc)`
      - `on_error` 自身抛:再 `log.exception` 一次,不让异常进未观察 future
    """
    fut = asyncio.run_coroutine_threadsafe(coro, loop)

    def _on_done(f: asyncio.Future[T]) -> None:
        try:
            result = f.result()
        except BaseException as exc:  # noqa: BLE001  # asyncio.CancelledError 也是 BaseException
            log.exception("%s failed", error_label)
            if on_error is not None:
                try:
                    on_error(exc)
                except Exception:  # noqa: BLE001
                    log.exception("on_error callback for %s raised", error_label)
            return
        if on_success is not None:
            try:
                on_success(result)
            except Exception:  # noqa: BLE001
                log.exception("on_success callback for %s raised", error_label)

    fut.add_done_callback(_on_done)
    return fut
