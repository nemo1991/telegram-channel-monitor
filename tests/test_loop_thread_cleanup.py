"""LoopThread cleanup 契约回归 —— 防止跨 test 残留 daemon loop。

2026-11-13:`test_list_joined_waits_for_state_to_become_ready_via_telethon_client`
在 GH Actions macOS runner 连续两次偶发 TimeoutError,teardown 同时报
`LoopThread cleanup timeout: thread still alive after 10.0s — daemon leak`。
根因是 `LoopThread.stop()` 只做 stop → join → close,没做文件头注释里早就
承诺的 cancel → drain。这两个用例把 cleanup 契约钉死,避免以后再退化。
"""

from __future__ import annotations

import asyncio
import threading

from tests.fixtures._loop_thread import LoopThread


def test_stop_cancels_pending_task_and_joins_thread() -> None:
    """stop() 必须 cancel + drain 残留 task,join 上 thread 并 close loop。"""
    lt = LoopThread()
    started = threading.Event()
    cancelled = threading.Event()

    async def _pending() -> None:
        """模拟上一个 test 超时后留在 qloop 上的 coroutine。"""
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    fut = asyncio.run_coroutine_threadsafe(_pending(), lt.loop)
    assert started.wait(timeout=2.0), "pending task 没在后台 loop 上跑起来"

    lt.stop()

    assert cancelled.is_set(), "stop() 没有 cancel 残留 task"
    assert fut.done(), "残留 task 的 future 没有被收尾"
    assert not lt._thread.is_alive(), "LoopThread 的 daemon thread 没退出"
    assert lt.loop.is_closed(), "LoopThread 的 loop 没被 close"


def test_repeated_stop_leaves_no_alive_loop_threads() -> None:
    """连续创建/停止多个 LoopThread 后,本批创建的 thread 全部退出。"""
    threads: list[threading.Thread] = []
    for _ in range(5):
        lt = LoopThread()
        fut = asyncio.run_coroutine_threadsafe(asyncio.sleep(0), lt.loop)
        fut.result(timeout=2.0)
        lt.stop()
        threads.append(lt._thread)

    leaked = [t.name for t in threads if t.is_alive()]
    assert not leaked, f"LoopThread 残留 daemon thread: {leaked}"
