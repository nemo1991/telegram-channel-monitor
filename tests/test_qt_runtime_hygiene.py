"""Qt 测试运行时治理契约 — 防止退回「每 test 起后台 daemon loop」。

承接 PR #37 归因:6.12 shutdown SIGSEGV 的直接触发者是测试侧跨线程 loop
fixture + widget 累积。本文件钉死三条契约:

1. 全会话只共享 **1 个** 正在运行的后台 loop(`qloop` / `bg_loop` /
   `loop_thread.asyncio_loop` 是同一个对象);
2. 残留 task 能被 drain 掉,且 drain 后 loop 仍可调度;
3. 顶层 widget 经 `_cleanup_top_level_widgets()` 后从 QWidget 树消失
   (走 `deleteLater`,不触发生产 `closeEvent`)。
"""

from __future__ import annotations

import asyncio
import threading

from PySide6.QtWidgets import QApplication, QWidget

from tests.fixtures import _qt_runtime


def test_all_loop_fixtures_share_one_running_loop(qloop, bg_loop, loop_thread) -> None:
    assert qloop is bg_loop
    assert qloop is loop_thread.asyncio_loop
    assert _qt_runtime._get_shared().loop is qloop
    assert qloop.is_running(), "共享 loop 必须真在后台线程跑 run_forever"


def test_only_one_shared_loop_thread_is_alive(qloop) -> None:
    """不允许再出现「N 个 test → N 个 daemon loop 线程」。"""
    alive = [t for t in threading.enumerate() if t.name.startswith("LoopThread-")]
    assert len(alive) == 1, f"应只有 1 个共享后台 loop 线程,实际 {len(alive)}: {alive}"


def test_drain_cancels_pending_task_and_keeps_loop_usable(qloop) -> None:
    started = threading.Event()

    async def _hang() -> None:
        started.set()
        await asyncio.Event().wait()

    fut = asyncio.run_coroutine_threadsafe(_hang(), qloop)
    assert started.wait(timeout=2.0), "残留协程没被调度到共享 loop"

    _qt_runtime._drain_shared_loop()

    probe = asyncio.run_coroutine_threadsafe(asyncio.sleep(0, result="ok"), qloop)
    assert probe.result(timeout=2.0) == "ok", "drain 后共享 loop 必须仍可调度"
    assert fut.done() or fut.cancelled()


def test_cleanup_removes_top_level_widgets(qapp) -> None:
    def _names() -> list[str]:
        out: list[str] = []
        for widget in QApplication.topLevelWidgets():
            try:
                out.append(widget.objectName())
            except RuntimeError:
                # C++ 对象已删,Python wrapper 失效 — 不计入
                pass
        return out

    probe = QWidget()
    probe.setObjectName("qt-runtime-hygiene-probe")
    assert "qt-runtime-hygiene-probe" in _names()

    _qt_runtime._cleanup_top_level_widgets()

    assert "qt-runtime-hygiene-probe" not in _names(), "顶层 widget 没被 deleteLater 释放"
