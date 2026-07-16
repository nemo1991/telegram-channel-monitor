"""MainWindow closeEvent + shutdown callback 行为。

覆盖关键回归点:
- 关窗 → closeEvent 同步跑 shutdown 协程(协程跑在外部 loop 线程)
- 没挂 cb 时保持默认行为(Qt 接受 close)
- cb 抛异常不阻止 quit(只 log,不弹框)
- cb 慢于 closeEvent 的轮询上限时,closeEvent 不挂死

注:closeEvent 里 `run_coroutine_threadsafe(...).result(timeout=...)` 需要目标
loop 在另一个线程上跑(否则 closeEvent 同步等时,loop 永远不会被 tick —
Qt 在 closeEvent 处理过程中不 pump 自己的事件)。这里用后台线程 + QApplication
processEvents 模拟生产(qasync 主线程 loop 在另一个线程里)。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from tgmonitor.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeMainWindow(MainWindow):
    """绕开真 MainWindow __init__ 的复杂依赖,只验 closeEvent 自己的逻辑。"""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        QMainWindow.__init__(self)
        self.loop = loop
        self._shutdown_cb = None


class _LoopThread:
    """在后台线程跑一个 asyncio loop,模拟 qasync 的 QEventLoop。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=2.0)

    @property
    def asyncio_loop(self) -> asyncio.AbstractEventLoop:
        return self.loop


@pytest.fixture
def loop_thread():
    lt = _LoopThread()
    yield lt
    lt.stop()


# ---- 默认路径:没挂 cb → 直接关 ----


def test_close_without_callback_accepts_event(qapp, loop_thread):
    win = _FakeMainWindow(loop_thread.asyncio_loop)
    assert win._shutdown_cb is None
    win.close()  # 不应抛


# ---- 挂 cb 后:closeEvent 同步阻塞跑协程 ----


def test_close_runs_shutdown_callback(qapp, loop_thread):
    """挂的 shutdown 协程必须被同步等待完成,而不是 fire-and-forget。"""
    calls: list[str] = []

    async def cb() -> None:
        await asyncio.sleep(0.05)
        calls.append("ran")

    win = _FakeMainWindow(loop_thread.asyncio_loop)
    win.set_shutdown_callback(cb)
    win.close()
    assert calls == ["ran"], f"shutdown 没被调:calls={calls}"


def test_close_propagates_callback_exception_but_still_quits(qapp, loop_thread):
    """shutdown 抛异常时,closeEvent 不应再弹框或阻止 Qt 退出。"""

    async def cb() -> None:
        raise RuntimeError("aiotdlib close failed")

    win = _FakeMainWindow(loop_thread.asyncio_loop)
    win.set_shutdown_callback(cb)
    win.close()  # 必须不抛


def test_close_callback_slow_does_not_hang(qapp, loop_thread):
    """shutdown 慢(>10s)时,closeEvent 限时轮询 → 不挂死,记 warning 放行。"""
    started = time.monotonic()
    task_holder: dict[str, asyncio.Task | None] = {"t": None}

    async def cb() -> None:
        # 把自己注册出去,测试结束时 cancel,免得留下 pending Task 警告
        task_holder["t"] = asyncio.current_task()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass

    try:
        win = _FakeMainWindow(loop_thread.asyncio_loop)
        win.set_shutdown_callback(cb)
        win.close()
        elapsed = time.monotonic() - started
        # closeEvent 上限 10s + 余量。cb 永远完不成 → closeEvent 应在 ~10s 后放弃
        assert elapsed < 13.0, f"closeEvent 应该限时 ~10s,实跑 {elapsed:.1f}s"
    finally:
        # cancel 慢任务 — closeEvent 放弃后任务还挂着,不 cancel 会在 fixture
        # 强 stop loop 时产生 "Task was destroyed but it is pending" 警告
        t = task_holder.get("t")
        if t is not None and not t.done():
            loop_thread.asyncio_loop.call_soon_threadsafe(t.cancel)
            # 给 cancel 一点时间传播
            time.sleep(0.1)


def test_close_succeeds_when_callback_returns_quickly(qapp, loop_thread):
    """cb 短协程 → closeEvent 在第一个轮询周期就拿到结果,放行。"""
    calls: list[str] = []

    async def cb() -> None:
        calls.append("enter")
        # 短到 closeEvent 第一次 processEvents 就能跑完
        await asyncio.sleep(0)

    win = _FakeMainWindow(loop_thread.asyncio_loop)
    win.set_shutdown_callback(cb)
    started = time.monotonic()
    win.close()
    elapsed = time.monotonic() - started
    assert calls == ["enter"]
    assert elapsed < 1.0, f"短 cb 应秒关,实跑 {elapsed:.1f}s"


# ---- set_shutdown_callback 自身 ----


def test_set_shutdown_callback_stores(qapp, loop_thread):
    win = _FakeMainWindow(loop_thread.asyncio_loop)
    assert win._shutdown_cb is None

    async def cb() -> None:
        pass

    win.set_shutdown_callback(cb)
    assert win._shutdown_cb is cb


# ---- 协程被 cancel 路径(用户连按 cmd+Q / loop shutdown) ----


def test_close_handles_cancelled_coroutine_without_promoting_to_qt(qapp, loop_thread):
    """回归:`concurrent.futures.CancelledError` 是 BaseException 不是 Exception,
    closeEvent 必须在收尾时单独接,否则会从 closeEvent 抛回 → Qt 报
    "Error calling Python override of QMainWindow::closeEvent()"。
    """
    task_holder: dict[str, asyncio.Task | None] = {"t": None}

    async def cb() -> None:
        task_holder["t"] = asyncio.current_task()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # closeEvent 已主动 cancel 我们,正常退出
            return

    try:
        win = _FakeMainWindow(loop_thread.asyncio_loop)
        win.set_shutdown_callback(cb)
        # 启动 cb(挂到后台 loop);在 closeEvent 之前先 cancel 它,模拟
        # loop shutdown / 用户多次 quit 的真实路径:cancel 在 closeEvent 之前
        # 就已发生,closeEvent 拿到的是个已 cancelled 的 future。
        loop_thread.asyncio_loop.call_soon_threadsafe(task_holder.__setitem__, "t", None)
        # 先把 cb 调起来
        fut_for_setup = asyncio.run_coroutine_threadsafe(
            cb(), loop_thread.asyncio_loop
        )
        # 等 cb 把自己注册到 holder 上(loop 跑几个 tick)
        deadline_setup = time.monotonic() + 1.0
        while task_holder["t"] is None and time.monotonic() < deadline_setup:
            time.sleep(0.02)
        # 主动 cancel
        t = task_holder["t"]
        assert t is not None
        loop_thread.asyncio_loop.call_soon_threadsafe(t.cancel)
        # 给 cancel 一点时间让 future 状态切到 cancelled
        time.sleep(0.1)
        # 关键断言:closeEvent 不应抛 — 走到这里如果异常未被吃,
        # pytest 会以 "Error calling Python override" / CancelledError 失败
        win.close()
        assert fut_for_setup.done() or fut_for_setup.cancelled()
    finally:
        t = task_holder.get("t")
        if t is not None and not t.done():
            loop_thread.asyncio_loop.call_soon_threadsafe(t.cancel)
            time.sleep(0.1)