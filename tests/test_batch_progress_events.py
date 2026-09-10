"""2026-09-09 v1.7.2:BatchProgress / BatchDone 事件 → VM signal 透传测试。

覆盖:
- Bus.publish(BatchProgress) → MonitorViewModel.batch_progress Qt signal 触发
- Bus.publish(BatchDone) → MonitorViewModel.batch_done Qt signal 触发
- payload 在 signal 里完整保留(op / processed / total / succeeded / failed)
- 其它 Event 类型不会触发 batch signal(类型过滤)

模式:mock 出 AppService + MonitorService 注入 VM,直接走 .bus.publish,监听
signal payload 验证。
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QCoreApplication

from tgmonitor.core.events import (
    BatchDone,
    BatchProgress,
    EventBus,
    MessageEdited,
)
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel


@pytest.fixture
def qapp() -> QCoreApplication:
    """Qt app 单例 — QObject Signal 需要 event loop 实例。"""
    app = QCoreApplication.instance() or QCoreApplication([])
    return app  # type: ignore[return-value]


@pytest.fixture
def vm(qapp: QCoreApplication) -> MonitorViewModel:
    """最小可用的 MonitorViewModel — AppService / MonitorService 用 MagicMock。"""
    bus = EventBus()
    app = MagicMock()
    app.bus = bus
    monitor = MagicMock()
    loop = asyncio.new_event_loop()
    return MonitorViewModel(app=app, monitor=monitor, loop=loop)


def _drain(qapp: QCoreApplication) -> None:
    """Qt signal 是 queued — 需要 processEvents 跑掉 pending callbacks。"""
    qapp.processEvents()


@pytest.mark.asyncio
async def test_bus_batch_progress_triggers_vm_signal(
    vm: MonitorViewModel, qapp: QCoreApplication
) -> None:
    """Bus.publish(BatchProgress) → MonitorViewModel.batch_progress signal 触发。"""
    captured: list[BatchProgress] = []
    vm.batch_progress.connect(lambda e: captured.append(e))  # type: ignore[arg-type]

    evt = BatchProgress(op="delete", processed=3, total=10)
    await vm.app.bus.publish(evt)
    _drain(qapp)

    assert len(captured) == 1
    assert captured[0] is evt
    assert captured[0].op == "delete"
    assert captured[0].processed == 3
    assert captured[0].total == 10


@pytest.mark.asyncio
async def test_bus_batch_done_triggers_vm_signal(
    vm: MonitorViewModel, qapp: QCoreApplication
) -> None:
    """Bus.publish(BatchDone) → MonitorViewModel.batch_done signal 触发。"""
    captured: list[BatchDone] = []
    vm.batch_done.connect(lambda e: captured.append(e))  # type: ignore[arg-type]

    evt = BatchDone(op="forward", succeeded=5, failed=0)
    await vm.app.bus.publish(evt)
    _drain(qapp)

    assert len(captured) == 1
    assert captured[0] is evt
    assert captured[0].op == "forward"
    assert captured[0].succeeded == 5


@pytest.mark.asyncio
async def test_vm_signal_filters_non_batch_events(
    vm: MonitorViewModel, qapp: QCoreApplication
) -> None:
    """MessageEdited 等非 batch 事件不应触发 batch_progress / batch_done。"""
    progress_captured: list = []
    done_captured: list = []
    vm.batch_progress.connect(lambda e: progress_captured.append(e))  # type: ignore[arg-type]
    vm.batch_done.connect(lambda e: done_captured.append(e))  # type: ignore[arg-type]

    await vm.app.bus.publish(MessageEdited(message=MagicMock()))
    _drain(qapp)

    assert progress_captured == []
    assert done_captured == []


@pytest.mark.asyncio
async def test_vm_multiple_batch_progress_preserve_order(
    vm: MonitorViewModel, qapp: QCoreApplication
) -> None:
    """连续 3 个 BatchProgress → signal 按顺序触发,processed 累加正确。"""
    captured: list[BatchProgress] = []
    vm.batch_progress.connect(lambda e: captured.append(e))  # type: ignore[arg-type]

    await vm.app.bus.publish(BatchProgress(op="pin", processed=1, total=5))
    await vm.app.bus.publish(BatchProgress(op="pin", processed=2, total=5))
    await vm.app.bus.publish(BatchProgress(op="pin", processed=5, total=5))
    _drain(qapp)

    assert len(captured) == 3
    assert [c.processed for c in captured] == [1, 2, 5]
    assert all(c.op == "pin" for c in captured)


@pytest.mark.asyncio
async def test_vm_batch_done_carries_error_field(
    vm: MonitorViewModel, qapp: QCoreApplication
) -> None:
    """顶层错误(paused short-circuit)走 BatchDone.error 字段透传。"""
    captured: list[BatchDone] = []
    vm.batch_done.connect(lambda e: captured.append(e))  # type: ignore[arg-type]

    await vm.app.bus.publish(BatchDone(op="forward", succeeded=0, failed=0, error="paused"))
    _drain(qapp)

    assert len(captured) == 1
    assert captured[0].error == "paused"
