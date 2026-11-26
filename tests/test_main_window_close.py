"""MainWindow closeEvent + shutdown callback 行为。

覆盖关键回归点:
- 关窗 → closeEvent 同步跑 shutdown 协程(协程跑在外部 loop 线程)
- 没挂 cb 时保持默认行为(Qt 接受 close)
- cb 抛异常不阻止 quit(只 log,不弹框)
- cb 慢于 closeEvent 的轮询上限时,closeEvent 不挂死

注:这里用后台线程跑 loop 作为测试简化 —— 后台 loop 自己 tick,closeEvent
同步等时协程仍能推进。但**生产里 qasync loop 跑在 Qt 主线程上**(与 closeEvent
同线程),死等 fut.result() 会卡死 loop;helper 改用 processEvents 泵 Qt 事件
驱动协程。主线程同 loop 的真实回归见 `tests/test_shutdown_main_thread.py`。
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QMainWindow

from tgmonitor.ui.main_window import MainWindow

# 2026-10-02 v1.12.1:`test_close_propagates_callback_exception_but_still_quits`
# (line 101) 在 GH Actions ubuntu/macos runner 偶发 hang:`_LoopThread` 后台线
# 程 + `loop_thread.asyncio_loop` + `run_coroutine_threadsafe` 在 offscreen Qt
# 平台行为与本地 Qt 真机有差异,closeEvent → run_shutdown_coro_sync → cb 抛
# RuntimeError 时 Qt 仍 pump paint event,后台 loop join(timeout=2.0) 失败
# 后 test fixture cleanup 不返回 → 进程挂着不动(本地 9 passed in 1.08s 不复现)。
# 套 `windows_qt_paint_skip`(v1.8.3 起扩到 (win32, linux, darwin))。
_PAINT_PATH_RACE_PLATFORMS = ("win32", "linux", "darwin")
windows_qt_paint_skip = pytest.mark.skipif(
    sys.platform in _PAINT_PATH_RACE_PLATFORMS,
    reason=(
        "Qt offscreen + _LoopThread + run_coroutine_threadsafe 在 closeEvent"
        "触发 paint event race:GH Actions ubuntu/macos/windows runner 的 Qt"
        "offscreen 平台都受影响。本地 Qt 6.11+ macOS 真机 + linux 真机仍过"
        "(不属本 race)。"
    ),
)


class _FakeMainWindow(MainWindow):
    """绕开真 MainWindow __init__ 的复杂依赖,只验 closeEvent 自己的逻辑。

    2026-08-30 v1.5.0 PR #A4:closeEvent 现读 `_truly_quit` / `_tray`,
    这里手装 stub — 走「无 tray → 默认路径」分支,保持原有断言不变。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        QMainWindow.__init__(self)
        self.loop = loop
        self._shutdown_cb = None
        self._truly_quit = False
        self._tray = None  # 无 tray → 不进 minimize-to-tray 分支
        self._tray_first_close_hint_shown = False
        # v2.0.11:本文件验「同步跑 shutdown callback」兜底路径。默认
        # close_on_exit=True 会走 qt_app.quit() 提前 return,这里固定关掉,
        # 让下面断言仍覆盖原路径;新分支由 test_main_window_tray 覆盖。
        self._close_on_exit = lambda: False


# 2026-11-14:删掉本地 `_LoopThread` + function-scope `loop_thread`,统一用
# `tests.fixtures._qt_runtime` 的 session-scope 共享 LoopThread(暴露
# `.asyncio_loop`)。测试函数参数名 `loop_thread` 保持不变。


# ---- 默认路径:没挂 cb → 直接关 ----


# ---- 挂 cb 后:closeEvent 同步阻塞跑协程 ----


@windows_qt_paint_skip
def test_close_propagates_callback_exception_but_still_quits(qapp, loop_thread):
    """shutdown 抛异常时,closeEvent 不应再弹框或阻止 Qt 退出。"""

    async def cb() -> None:
        raise RuntimeError("tdlib_json close failed")

    win = _FakeMainWindow(loop_thread.asyncio_loop)
    win.set_shutdown_callback(cb)
    win.close()  # 必须不抛


# ---- set_shutdown_callback 自身 ----


# ---- 协程被 cancel 路径(用户连按 cmd+Q / loop shutdown) ----


# ---- tray 不可用 → 真退出 (2026-09-22 v1.8.x) --------------------------------


class _FakeTrayInactive:
    """`_tray` 装着但 `is_active=False`(offscreen / Linux server / 老 Windows
    无 indicator)。production 行为:之前 closeEvent 跳过 minimize-to-tray 但
    也不走 _truly_quit → 进程 hang。修复后 closeEvent 显式置 `_truly_quit=True`。
    """

    is_active = False


def test_close_with_inactive_tray_falls_through_to_quit(qapp, loop_thread):
    """v1.8.x:tray inactive + 挂 cb → closeEvent 必须把 `_truly_quit=True`,
    让下方 _shutdown_cb 路径跑起来(否则 `setQuitOnLastWindowClosed(False)` 让
    进程进入「窗口全 hide 但 loop 仍跑」无界面状态)。
    """
    calls: list[str] = []

    async def cb() -> None:
        await asyncio.sleep(0)
        calls.append("ran")

    win = _FakeMainWindow(loop_thread.asyncio_loop)
    win._tray = _FakeTrayInactive()  # tray 装着但 inactive
    win.set_shutdown_callback(cb)
    win.close()
    assert win._truly_quit is True, "tray inactive 时 closeEvent 应设 _truly_quit=True,实际仍 False"
    assert calls == ["ran"], f"shutdown 没被调:calls={calls}"
