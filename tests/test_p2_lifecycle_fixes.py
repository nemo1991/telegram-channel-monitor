"""P2 生命周期修复回归测试 — 2026-11-25 #34。

`orchestrator.stop()`:主 task `wait_for(2.0)` 超时后 cancel,旧实现
cancel 后不 await — task 异常无人观察("Task exception was never
retrieved"),cancel 后的清理也没人等。新实现补
`await asyncio.gather(rt.task, return_exceptions=True)`(包 try)。
"""

from __future__ import annotations

import asyncio
import time

from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.core.telegram.fake_client import FakeTelegramClient


class _CleanupStream:
    """`__anext__` 永挂,被 cancel 时模拟清理耗时后正常收尾。

    验证点:stop() 必须等到清理完成(cancel 后 gather),而不是
    cancel() 一发出就返回。
    """

    def __init__(self) -> None:
        self.cleaned = asyncio.Event()

    async def aclose(self) -> None:  # pragma: no cover — stop 先走 task 分支
        pass

    async def __anext__(self):
        try:
            await asyncio.sleep(3600)  # 永挂 — 触发 stop 的 2s wait_for 超时
        except asyncio.CancelledError:
            # 被 cancel:模拟清理耗时,然后正常结束循环(StopAsyncIteration)
            self.cleaned.set()
            await asyncio.sleep(0.05)
            raise StopAsyncIteration from None
        raise StopAsyncIteration  # pragma: no cover — 不可达


class _HangingStreamClient(FakeTelegramClient):
    """subscribe_updates 返回自管 stream(不走内部队列)。"""

    def __init__(self) -> None:
        super().__init__()
        self._stream: _CleanupStream | None = None

    def subscribe_updates(self):  # type: ignore[override]
        self._stream = _CleanupStream()
        return self._stream


async def test_orchestrator_stop_awaits_task_after_cancel(bus, storage, objectstore, settings):
    """P2 #34 回归:主 task 超时 cancel 后,stop 必须等 task 真正退出。

    - 旧实现:cancel() 后直接 rt.task=None 返回 — 清理事件未 set;
    - 新实现:await gather(return_exceptions=True) — 清理完成后 stop 才返回。
    """
    client = _HangingStreamClient()
    mon = MonitorService(bus, client, storage, objectstore, settings)
    mon.set_whitelist([100])
    await mon.start()
    # 让主 task 真正进入 anext 挂起点(否则 stop.set() 会让 _run 在首个
    # while 检查处直接退出,测不到 wait_for 超时 → cancel → await 路径)
    await asyncio.sleep(0.05)
    started = time.monotonic()
    await mon.stop()
    elapsed = time.monotonic() - started

    # 2s wait_for 超时 + cancel 后清理(0.05s)完成才返回
    assert elapsed >= 2.0, f"应先经历 2s wait_for 超时,实跑 {elapsed:.2f}s"
    stream = client._stream
    assert stream is not None
    assert stream.cleaned.is_set(), "cancel 后的清理必须被 await(P2 #34)— stop 返回时清理已完成"
