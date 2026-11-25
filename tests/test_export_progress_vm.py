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

from tgmonitor.core.events import EventBus
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


def test_cancel_current_export_noop_when_no_task() -> None:
    """未启动 export 时 cancel 必须安全(无 task 引用)。"""
    vm = _make_vm()
    assert vm._export_task is None
    vm.cancel_current_export()  # 不抛
