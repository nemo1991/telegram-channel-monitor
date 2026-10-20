"""`tgmonitor.ui.viewmodels` — MonitorViewModel 多 mixin 拆分(2026-10-20 plan-batch9-η)。

公开 API:`MonitorViewModel` 唯一类,33 Signal + 17 public method + 3
状态字段 1:1 保留原 `viewmodels/monitor_vm.py:MonitorViewModel`(839 LOC
单类,2026-10-20 拆 5 mixin + BusMixin 收口)。

`viewmodels.monitor_vm` 直接 import 路径仍兼容(2026-10-20 plan-batch9-η
起 facade 仅 ~70 LOC,作为入口壳存在),但建议外部从本 `__init__` re-export
取类,后续如需进一步下沉 facade(plan 目标 ~50 LOC)再调整。

`from tgmonitor.ui.viewmodels import MonitorViewModel` 等价于
`from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel`。
"""

from __future__ import annotations

from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel

__all__ = ["MonitorViewModel"]
