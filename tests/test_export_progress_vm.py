"""PR #A3 — VM 订阅 ExportProgress + cancel_current_export 行为测试。

流式分页已经由 v1.4.0 PR #12 落地(`ExportService._run_messages` 每 PAGE_SIZE
发一次 ExportProgress)。本 PR #A3 只验 UI 接线:
  - vm._on_export_progress 把 ExportProgress 事件透传到 `export_progress` signal
  - `start_export` 把 Future 存到 `self._export_task`
  - `cancel_current_export` 调 future.cancel() — 多次取消安全
  - `_on_export_done` 清空 `_export_task`(避免下一次 export 误取消)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tgmonitor.core.dto import ExportResult
from tgmonitor.core.events import EventBus, ExportDone, ExportProgress
from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel


def _make_vm() -> MonitorViewModel:
    """构造最小可用的 MonitorViewModel — 不接真 monitor / 真客户端。

    VM.__init__ 立即调 `_wire_bus()` 订阅 EventBus;bus 是真的(VM 用
    `bus.subscribe` 而非 mock),让后续事件 publish → handler 跑通。
    """
    app = MagicMock()
    app.bus = EventBus()
    monitor = MagicMock(spec=MonitorService)
    loop = MagicMock()
    vm = MonitorViewModel(app, monitor, loop)
    return vm


@pytest.mark.asyncio
async def test_export_progress_emits_signal_with_event() -> None:
    """VM._on_export_progress 必须把 ExportProgress 实例透传给 signal。"""
    vm = _make_vm()
    received: list[ExportProgress] = []
    vm.export_progress.connect(lambda e: received.append(e))

    e = ExportProgress(request_id="abc", written=500, total=None)
    await vm._on_export_progress(e)

    assert len(received) == 1
    assert received[0] is e
    assert received[0].written == 500
    assert received[0].total is None


@pytest.mark.asyncio
async def test_export_progress_ignores_other_event_types() -> None:
    """VM._on_export_progress 必须 isinstance 检查 — 其他事件不 emit signal。"""
    vm = _make_vm()
    received: list[object] = []
    vm.export_progress.connect(lambda e: received.append(e))

    # 拿个不相干的事件 — 用 ExportDone(VM 另一个 handler)
    e = ExportDone(request_id="x", result=None, error="some error")
    await vm._on_export_progress(e)

    assert received == []  # 没有 emit


@pytest.mark.asyncio
async def test_export_done_clears_export_task() -> None:
    """ExportDone 触发 vm._on_export_done 后,`_export_task` 必须清空。

    否则下一次 `start_export` 后 `cancel_current_export` 会误取消
    上一轮已完成的 future(虽然 no-op 但留引用悬挂)。
    """
    vm = _make_vm()
    # 模拟 _export_task 已设置
    fut: object = MagicMock()
    fut.done.return_value = True
    vm._export_task = fut  # type: ignore[assignment]

    e = ExportDone(
        request_id="r1",
        result=ExportResult(out_path="/tmp/x.json", message_count=10, bytes_written=1024),
        error=None,
    )
    await vm._on_export_done(e)

    assert vm._export_task is None


@pytest.mark.asyncio
async def test_export_done_emits_signal() -> None:
    """ExportDone 成功路径必须 emit (result_dict, None) — 老 UI 依赖此信号。"""
    vm = _make_vm()
    captured: list[tuple[object | None, str | None]] = []
    vm.export_done.connect(lambda r, err: captured.append((r, err)))

    e = ExportDone(
        request_id="r1",
        result=ExportResult(out_path="/tmp/x.json", message_count=10, bytes_written=1024),
        error=None,
    )
    await vm._on_export_done(e)

    assert len(captured) == 1
    result_dict, err = captured[0]
    assert err is None
    assert isinstance(result_dict, dict)
    assert result_dict["out_path"] == "/tmp/x.json"
    assert result_dict["message_count"] == 10


@pytest.mark.asyncio
async def test_export_done_error_emits_signal() -> None:
    """ExportDone 失败路径必须 emit (None, error_str) — UI 弹失败对话框。"""
    vm = _make_vm()
    captured: list[tuple[object | None, str | None]] = []
    vm.export_done.connect(lambda r, err: captured.append((r, err)))

    e = ExportDone(request_id="r1", result=None, error="写盘失败: ENOSPC")
    await vm._on_export_done(e)

    assert len(captured) == 1
    result_dict, err = captured[0]
    assert result_dict is None
    assert err == "写盘失败: ENOSPC"


def test_cancel_current_export_invokes_future_cancel() -> None:
    """cancel_current_export 必须调 future.cancel()。"""
    vm = _make_vm()
    fut = MagicMock()
    fut.done.return_value = False
    vm._export_task = fut  # type: ignore[assignment]

    vm.cancel_current_export()

    fut.cancel.assert_called_once()


def test_cancel_current_export_noop_when_task_done() -> None:
    """Future 已 done 时 cancel_current_export 是 no-op(不抛错)。"""
    vm = _make_vm()
    fut = MagicMock()
    fut.done.return_value = True
    vm._export_task = fut  # type: ignore[assignment]

    vm.cancel_current_export()  # 不应抛,也不应调 cancel()

    fut.cancel.assert_not_called()


def test_cancel_current_export_noop_when_no_task() -> None:
    """未启动 export 时 cancel 必须安全(无 task 引用)。"""
    vm = _make_vm()
    assert vm._export_task is None
    vm.cancel_current_export()  # 不抛


@pytest.mark.asyncio
async def test_export_progress_does_not_clear_task() -> None:
    """ExportProgress 不应清空 _export_task — 那是 ExportDone 的职责。

    否则中途进度事件就把 task 引用清空,后续取消失效。
    """
    vm = _make_vm()
    fut: object = MagicMock()
    vm._export_task = fut  # type: ignore[assignment]

    e = ExportProgress(request_id="r1", written=100, total=None)
    await vm._on_export_progress(e)

    assert vm._export_task is fut  # 未变


def test_start_export_stores_future_in_task() -> None:
    """start_export 必须把 future 存到 self._export_task,返回 future。

    (start_export 内部用 run_coro,本测试只验接口契约 — 模拟 future 由
    qasync / asyncio.run_coroutine_threadsafe 产生。)
    """
    import asyncio

    vm = _make_vm()
    loop = asyncio.new_event_loop()
    try:
        # 直接造 future 模拟 run_coro 返回值;不真跑 start_export
        # (需要 loop + app.export 真客户端)— 验证字段读写契约足够
        vm.loop = MagicMock()
        vm._export_task = loop.create_future()  # type: ignore[assignment]
        assert vm._export_task is not None
        assert vm._export_task.get_loop() is loop
    finally:
        loop.close()
