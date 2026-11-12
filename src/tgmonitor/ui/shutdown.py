"""Shutdown coroutine synchronizer — Qt 主线程上同步等 shutdown 协程完成。

2026-10-01 v1.12.x 从 `main_window.py` 抽出复用 — `MainWindow.closeEvent` 与
`app.py:aboutToQuit` handler 共用同一份同步等协程 helper。

关键约束:**qasync 的 loop 就跑在 Qt 主线程上**(与调用本 helper 的线程同一个)。
qasync 的 asyncio 回调由 Qt timer 事件 / queued signal 驱动(见 `_SimpleTimer`),
只有 Qt 事件循环 pump 时才会 tick。因此:

- **不能** `fut.result(timeout)` 死等 —— 那会阻塞唯一能 tick loop 的线程,
  协程永远不跑 → 必然超时(历史 bug:每次退出卡 8s 且 client/storage 未关)。
- **不能**嵌套 `QEventLoop.exec()` —— macOS 26 arm64 / Windows 真机偶发 segfault。
- 正解:等 future 的同时用 `QApplication.processEvents()` 泵 Qt 事件驱动 qasync,
  排除用户输入事件避免 shutdown 期间重复触发 quit,带 hard deadline 兜底。

任何意外(RuntimeError / CancelledError / Exception)由调用方 try/except 兜底
— 此 helper 不抛(只 log warning)。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from typing import Any, Awaitable, Callable, Coroutine, cast

log = logging.getLogger(__name__)


def run_shutdown_coro_sync(
    loop: asyncio.AbstractEventLoop,
    cb: Callable[[], Awaitable[None]],
    *,
    deadline_ms: int = 10_000,
) -> None:
    """同步地跑一个 shutdown 协程 — Qt 主线程上,泵 Qt 事件等 future 完成。

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

    # qasync loop 与调用方同线程:阻塞 fut.result() 会卡死 loop → 协程不跑。
    # 改为泵 Qt 事件(qasync 的 asyncio 回调由 Qt timer / queued signal 驱动),
    # 不嵌套 QEventLoop.exec()(历史 segfault 源),排除用户输入避免重复 quit。
    from PySide6.QtCore import QEventLoop
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    deadline = time.monotonic() + deadline_ms / 1000
    timed_out = False
    while not fut.done():
        if time.monotonic() >= deadline:
            timed_out = True
            break
        if app is not None:
            app.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 50)
        # 让出 CPU,避免无事件时 busy-spin;同时设定轮询粒度(~5ms)。
        time.sleep(0.005)

    if timed_out:
        log.warning("shutdown timed out after %.1fs; cancelling", deadline_ms / 1000)
        fut.cancel()

    # 观察结果以归一异常日志;future 此刻通常已 done(或超时后 cancel 在途)。
    try:
        fut.result(timeout=0)
    except concurrent.futures.TimeoutError:
        pass  # 超时已 cancel,异步 cancel 可能尚未落地
    except concurrent.futures.CancelledError:
        log.warning("shutdown coroutine was cancelled")
    except Exception as exc:  # noqa: BLE001
        log.warning("shutdown raised: %s: %s", type(exc).__name__, exc)
