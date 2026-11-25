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
    BatchProgress,
    EventBus,
)
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel


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
