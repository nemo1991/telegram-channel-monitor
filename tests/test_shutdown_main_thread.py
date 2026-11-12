"""主线程 qasync loop 下 shutdown 不死锁的回归测试(issue2「无法正常退出」)。

根因:qasync 的 loop 跑在 Qt 主线程上,`closeEvent` / `aboutToQuit` 也在主线程。
旧 `run_shutdown_coro_sync` 用 `fut.result(timeout)` 死等 → 阻塞唯一能 tick loop
的线程 → shutdown 协程永不执行 → 每次退出卡满 deadline(生产 8s)且 client /
storage 未关。`tests/test_main_window_close.py` 用后台线程跑 loop,恰好掩盖了
这个同线程死锁。

本测试在**真实 qasync loop + run_forever** 内,从 Qt timer 回调(模拟
aboutToQuit)调 `run_shutdown_coro_sync`,断言协程真在主线程 loop 上跑完。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.ui.shutdown import run_shutdown_coro_sync  # noqa: E402


def _run_with_qasync(
    qapp: QApplication, trigger: Callable[[asyncio.AbstractEventLoop], None]
) -> None:
    """在真实 qasync 主线程 loop 内跑 trigger(loop),trigger 负责 quit。"""
    from qasync import QEventLoop as QAsyncLoop

    loop = QAsyncLoop(qapp)
    asyncio.set_event_loop(loop)
    # 保险:无论如何 3s 后强制 quit,避免任何意外挂死整个 test suite。
    QTimer.singleShot(3000, qapp.quit)
    QTimer.singleShot(0, lambda: trigger(loop))
    try:
        loop.run_forever()
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_shutdown_completes_on_main_thread_qasync_loop(qapp: QApplication) -> None:
    """协程必须在主线程 loop 上真跑完,而不是死等到超时(核心回归)。"""
    ran: list[str] = []
    elapsed: list[float] = []

    async def cb() -> None:
        ran.append("enter")
        await asyncio.sleep(0.01)
        ran.append("ran")

    def trigger(loop: asyncio.AbstractEventLoop) -> None:
        t0 = time.monotonic()
        run_shutdown_coro_sync(loop, cb, deadline_ms=2000)
        elapsed.append(time.monotonic() - t0)
        qapp.quit()

    _run_with_qasync(qapp, trigger)

    assert ran == ["enter", "ran"], f"shutdown 协程未在主线程 loop 上跑完(死锁): ran={ran}"
    assert elapsed[0] < 1.5, f"应秒级完成,实跑 {elapsed[0]:.2f}s — 疑似死等超时"


def test_shutdown_timeout_still_releases_on_main_thread(qapp: QApplication) -> None:
    """协程慢于 deadline 时,helper 限时 cancel 放行,不死锁、不抛。"""
    ran: list[str] = []
    elapsed: list[float] = []

    async def cb() -> None:
        ran.append("enter")
        await asyncio.sleep(5.0)  # 远大于注入的 200ms deadline
        ran.append("ran")

    def trigger(loop: asyncio.AbstractEventLoop) -> None:
        t0 = time.monotonic()
        run_shutdown_coro_sync(loop, cb, deadline_ms=200)
        elapsed.append(time.monotonic() - t0)
        qapp.quit()

    _run_with_qasync(qapp, trigger)

    assert ran == ["enter"], f"cb 应进入但被 deadline cancel,不该跑完: ran={ran}"
    assert 0.15 < elapsed[0] < 1.5, f"应在 ~0.2s 超时放行,实跑 {elapsed[0]:.2f}s"


def test_shutdown_callback_exception_does_not_raise_on_main_thread(qapp: QApplication) -> None:
    """协程抛异常时 helper 只 log,不抛回 Qt(否则 aboutToQuit 崩)。"""
    elapsed: list[float] = []

    async def cb() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("client.close failed")

    def trigger(loop: asyncio.AbstractEventLoop) -> None:
        t0 = time.monotonic()
        run_shutdown_coro_sync(loop, cb, deadline_ms=2000)  # 必须不抛
        elapsed.append(time.monotonic() - t0)
        qapp.quit()

    _run_with_qasync(qapp, trigger)

    assert elapsed[0] < 1.5, f"异常路径也应秒级返回,实跑 {elapsed[0]:.2f}s"


@pytest.mark.parametrize("deadline_ms", [200])
def test_shutdown_no_callback_entry_raise_is_swallowed(
    qapp: QApplication, deadline_ms: int
) -> None:
    """cb() 入口即抛(返回协程前)→ helper 兜底 log,不抛、不死锁。"""
    elapsed: list[float] = []

    def cb() -> None:
        raise ValueError("bad callback")

    def trigger(loop: asyncio.AbstractEventLoop) -> None:
        t0 = time.monotonic()
        run_shutdown_coro_sync(loop, cb, deadline_ms=deadline_ms)  # 必须不抛
        elapsed.append(time.monotonic() - t0)
        qapp.quit()

    _run_with_qasync(qapp, trigger)

    assert elapsed[0] < 1.0, f"入口异常应即时返回,实跑 {elapsed[0]:.2f}s"
