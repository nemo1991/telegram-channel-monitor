"""PR 1a:`FakeApp` 集中 — 原 `tests/test_monitor_vm.py:18` +
`tests/test_media_progress_signal.py:17` 重复定义,合并到此。

`FakeApp` 只暴露 `bus` 属性 — VM 只用这一个(其它 AppService 接口 stub)。
避免在 VM 测试里 mock 整个 AppService。
"""

from __future__ import annotations

from tgmonitor.core.events import EventBus


class FakeApp:
    """MonitorViewModel 测试用的最小 AppService stub。

    只暴露 VM 实际访问的属性 / 方法,其它都通过 `__getattr__` 兜底返 None
    (或对方法的 stub),避免新增 VM 调用面时这里忘改。

    现有使用点:
    - tests/test_monitor_vm.py — VM signal bridge 测试
    - tests/test_media_progress_signal.py — VM media progress 测试
    """

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

    def __getattr__(self, name: str):
        # VM 只用 bus,其它访问返 None — 让"调用错 API"显式 AttributeError
        # 而不是静默 None(避免 bug 掩盖)。
        raise AttributeError(f"FakeApp has no attribute {name!r}; VM should not access it")

    # VM 显式用到的非 bus 接口(如果有)放这里。
    # 当前 _make_vm 只用 bus,所以保持空。
