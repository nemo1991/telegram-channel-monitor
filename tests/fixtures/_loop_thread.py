"""共享 `_LoopThread` — 后台线程跑 asyncio loop,模拟 qasync 主线程 loop。

2026-10-03 v1.12.1:之前 4 个测试文件各持一份 inline `_LoopThread` 类,cleanup
行为各异(`join timeout=2.0` 太短,`loop.close()` 在 thread 还活着时被吞)→
多 test 间 daemon thread 残留 → 第二个 test 的 `run_coroutine_threadsafe`
调度到受污染 loop → setup_async 卡死。

集中到这一处统一行为,所有 4 个文件的 inline 版本替换为 `from tests.fixtures._loop_thread import LoopThread`。

关键约束:
- `stop()` 必须:1) cancel 所有 task → 2) drain(等 task 处理 CancelledError)
  → 3) stop loop → 4) join thread → 5) close loop
- `join timeout=10.0`(原 2.0 在 CI runner 上不够)
- 任一步失败不抛 — daemon thread 兜底,测试不因 cleanup 失败挂掉
- 不复用:每个 fixture 调用 new `LoopThread()`,避免跨 test 状态污染
"""

from __future__ import annotations

import asyncio
import logging
import threading

log = logging.getLogger(__name__)


class LoopThread:
    """后台线程跑一个持续运行的 asyncio loop — 模拟 qasync 的 QEventLoop。

    Usage:
        lt = LoopThread()
        try:
            # 在 lt.loop 上调度协程
            fut = asyncio.run_coroutine_threadsafe(coro, lt.loop)
            result = fut.result(timeout=10.0)
        finally:
            lt.stop()
    """

    # cleanup 步骤超时(秒)
    # 2026-10-03 v1.12.1:windows runner 慢,5s 不够 → thread is_alive=True →
    # daemon leak → 下一 test 调度到受污染 loop 卡死。10s 在 win runner 实测够。
    _JOIN_TIMEOUT_S = 10.0

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run,
            name=f"LoopThread-{id(self)}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        # 不调 asyncio.set_event_loop(self.loop) — 多个 LoopThread 同时跑会
        # 互相覆盖 thread-local 的 current loop,导致跨 thread 调度错乱。
        # 调用方用 `asyncio.run_coroutine_threadsafe(coro, lt.loop)` 显式传
        # loop 引用,不依赖 thread-local。
        self.loop.run_forever()

    def stop(self) -> None:
        """同步停止后台 loop — 必 join 上 + 必 close。

        顺序:1) call_soon_threadsafe(loop.stop) → 2) join thread → 3) close loop。
        加足够 join timeout(CI runner 慢,5s 才够)。
        若 join 超时,loop 还活着 → 不能 close,daemon thread 在 process exit 时回收。
        """
        if self.loop.is_closed():
            return
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except RuntimeError:
            # loop 已经在 close 中
            pass
        self._thread.join(timeout=self._JOIN_TIMEOUT_S)
        if self._thread.is_alive():
            log.warning(
                "LoopThread cleanup timeout: thread still alive after %.1fs — daemon leak",
                self._JOIN_TIMEOUT_S,
            )
            # loop 还在跑,close 会抛 RuntimeError("Event loop is running"),
            # 但 daemon thread 在 process exit 时被回收,这里不强 kill。
            return
        # thread 已死,close loop
        try:
            if not self.loop.is_closed():
                self.loop.close()
        except RuntimeError:
            pass
        except Exception:  # noqa: BLE001
            pass


__all__ = ["LoopThread"]
