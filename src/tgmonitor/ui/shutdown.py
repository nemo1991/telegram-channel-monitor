"""Shutdown coroutine synchronizer — Qt main thread 上同步阻塞等 future 完成。

2026-10-01 v1.12.x 从 `main_window.py` 抽出复用 — `MainWindow.closeEvent` 与
`app.py:aboutToQuit` handler 共用同一份同步等协程 helper。

关键约束:**helper 必须不在主线程 pump**。qasync 主线程 loop 与 Qt 同线程,
`fut.result()` 会阻塞主线程,不调 processEvents 也能让后台线程的 loop 推进
(因为 `run_coroutine_threadsafe` 已经把协程调度到独立 loop,后台线程自己 tick;
主线程只是等 fut 完成)。这条路径天然避开嵌套 QEventLoop 的所有 native race。

任何意外(RuntimeError / CancelledError / Exception)由调用方 try/except 兜底
— 此 helper 不抛(只 log warning)。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any, Awaitable, Callable, Coroutine, cast


log = logging.getLogger(__name__)


def run_shutdown_coro_sync(
    loop: asyncio.AbstractEventLoop,
    cb: Callable[[], Awaitable[None]],
    *,
    deadline_ms: int = 10_000,
) -> None:
    """同步阻塞地跑一个 shutdown 协程 — Qt 主线程上,真等 future 完成。

    Args:
        loop: shutdown 协程要跑的事件循环(qasync 主线程 loop)。
        cb: 同步入口(返回协程),内部 cast 为 Coroutine 调
            `run_coroutine_threadsafe`。
        deadline_ms: hard upper bound,默认 10s。tests 可缩到 200ms 验超时。
    """
    try:
        coro = cast(Coroutine[Any, Any, None], cb())
    except BaseException as exc:  # noqa: BLE001
        log.warning("shutdown callback raised on entry: %s: %s", type(exc).__name__, exc)
        return
    try:
        fut: concurrent.futures.Future[None] = asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError:
        log.warning("loop unavailable during shutdown")
        return

    try:
        fut.result(timeout=deadline_ms / 1000)
    except concurrent.futures.TimeoutError:
        log.warning("shutdown timed out after %.1fs; cancelling", deadline_ms / 1000)
        fut.cancel()
    except concurrent.futures.CancelledError:
        log.warning("shutdown coroutine was cancelled")
    except Exception as exc:  # noqa: BLE001
        log.warning("shutdown raised: %s: %s", type(exc).__name__, exc)