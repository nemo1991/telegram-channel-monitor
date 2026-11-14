"""Qt 测试运行时 — 全会话共享一个后台 loop + per-test widget 清理。

2026-11-14(承接 6.12 shutdown SIGSEGV 归因 PR #37):

原状:4 个测试文件各持一份 `_LoopThread` wrapper + function-scope
`qloop` / `qloop_16` / `loop_thread` fixture,`test_async_helper.py` 另有
`bg_loop`,**每个用例都新建一个后台 daemon 线程跑 `run_forever()`**;
`qapp` 是 session 单例且无 teardown,用例创建的窗口全部堆到进程退出。
PySide6 6.12.0 收紧解释器 shutdown 时 Qt 对象的销毁顺序后,这些残留对象
在 C++ 析构阶段随机 SIGSEGV(见 `scripts/diagnostics/pyside6_shutdown_probe.py`
与 PR #37:UI 三文件合跑 8/8 崩、非 Qt 四文件 8/8 正常、三文件各自单跑
15/15 正常,faulthandler 栈全落在测试侧跨线程 future 等待)。

治理:
1. 整个 pytest 会话只创建 **1 个** `LoopThread`(按需懒创建),所有需要
   「正在运行的 loop」的 fixture 共用它,不再每 test 起 daemon 线程;
2. 每个用例结束:drain 共享 loop 上残留的 task → `deleteLater()` 掉本用例
   创建的顶层 widget。**不调 `close()`** —— 生产 `MainWindow.closeEvent()`
   会走 `run_shutdown_coro_sync` 依赖活着的 event loop,PR #35 的 session
   teardown 正是因此在 Windows 报 RuntimeError;
3. `pytest_sessionfinish` 在会话结束、QApplication 仍存活时停掉 loop 线程,
   保证进程退出时没有活着的后台 loop 线程。

模块顶层刻意不 import PySide6 —— 纯 core 用例不应因 autouse fixture 被
强制拉起 Qt;清理函数里按需局部 import。
"""

from __future__ import annotations

import asyncio
import gc
import logging
from typing import TYPE_CHECKING

import pytest

from tests.fixtures._loop_thread import LoopThread

if TYPE_CHECKING:
    from collections.abc import Iterator

log = logging.getLogger(__name__)

# 会话级单例。懒创建:纯 core 用例若从不请求 qloop / loop_thread / bg_loop,
# 就不会有后台线程,也不 import PySide6。
_shared: LoopThread | None = None

_DRAIN_TIMEOUT_S = 2.0


def _get_shared() -> LoopThread:
    """返回(必要时创建)全会话唯一的后台 loop 线程。"""
    global _shared
    if _shared is None or _shared.loop.is_closed():
        _shared = LoopThread()
    return _shared


async def _cancel_pending() -> None:
    """取消共享 loop 上所有残留 task 并等它们处理完 CancelledError。"""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _drain_shared_loop() -> None:
    """清掉用例遗留的 coroutine / future,避免污染下一个用例的调度时序。"""
    lt = _shared
    if lt is None or lt.loop.is_closed():
        return
    fut = None
    try:
        fut = asyncio.run_coroutine_threadsafe(_cancel_pending(), lt.loop)
        fut.result(timeout=_DRAIN_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        if fut is not None:
            fut.cancel()
        # 不抛:cleanup 失败不应让用例变红,下一个用例仍能调度到活 loop。
        log.warning("shared Qt loop drain failed", exc_info=True)


def _cleanup_top_level_widgets() -> None:
    """用 deleteLater 释放本用例创建的顶层窗口,绕开 closeEvent 依赖。"""
    try:
        from PySide6.QtCore import QCoreApplication, QEvent  # noqa: PLC0415
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    except ImportError:
        return

    if QApplication.instance() is None:
        return
    widgets = QApplication.topLevelWidgets()
    if not widgets:
        return
    for widget in widgets:
        try:
            widget.deleteLater()
        except RuntimeError:
            # C++ 对象已被别处删除,Python wrapper 失效 — 忽略即可。
            pass
    try:
        # 只 pump DeferredDelete,不 pump paint/timer 事件,避免触发生产
        # closeEvent 或 Qt offscreen 的 paint race(CHANGELOG.md:1414)。
        QCoreApplication.sendPostedEvents(None, int(QEvent.Type.DeferredDelete))
        gc.collect()
    except RuntimeError:
        pass


# ---- 公共 fixture:替代 4 个测试文件里的本地 _LoopThread 定义 --------------


@pytest.fixture(scope="session")
def qloop() -> asyncio.AbstractEventLoop:
    """全会话共享的、正在后台线程运行的 asyncio loop。

    替代原先 function-scope 的 `qloop`(每 test 起一个 daemon 线程)。
    per-test drain 由 `_qt_runtime_hygiene` 保证,不会跨 test 污染。
    """
    return _get_shared().loop


@pytest.fixture(scope="session")
def qloop_16(qloop: asyncio.AbstractEventLoop) -> asyncio.AbstractEventLoop:
    """`test_channel_widget.py` 的历史命名别名,语义与 `qloop` 相同。"""
    return qloop


@pytest.fixture(scope="session")
def loop_thread() -> LoopThread:
    """`test_main_window_close.py` 用 `.asyncio_loop` 取 loop。"""
    return _get_shared()


@pytest.fixture(scope="session")
def bg_loop() -> asyncio.AbstractEventLoop:
    """`test_async_helper.py` 的 run_coro 测试用 — 同样复用共享 loop。"""
    return _get_shared().loop


# ---- per-test 清理 --------------------------------------------------------


@pytest.fixture(autouse=True)
def _qt_runtime_hygiene() -> Iterator[None]:
    """每个用例结束后:drain 共享 loop + 销毁顶层 widget。

    顺序刻意放在 teardown 尾部(autouse fixture 最先 setup → 最后 teardown),
    此时 QApplication 与后台 loop 线程都还活着,deleteLater 能安全生效。
    """
    yield
    _drain_shared_loop()
    _cleanup_top_level_widgets()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """会话结束、QApplication 仍存活时停掉唯一后台 loop 线程。

    停线程前再做一次 widget 清理,避免最后残留的对象被拖到解释器 shutdown。
    """
    global _shared
    try:
        _cleanup_top_level_widgets()
    finally:
        lt = _shared
        _shared = None
        if lt is not None:
            lt.stop()
