"""async helper — 2026-09-18 PR cleanup。

`wait_for(predicate, *, timeout=2.0, step=0.01)` 替代裸 `for+asyncio.sleep`
模式:每 step 调一次 predicate,True 立即返,False 继续;超时报 False。
比裸 sleep 短 + 比 bus.Event 简单(无需改 fixture / spy 注入)。
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


async def wait_for(
    predicate: Callable[[], T | Awaitable[T]],
    *,
    timeout: float = 2.0,
    step: float = 0.01,
) -> T | None:
    """poll `predicate()` 直到返真值或超时。

    `predicate` 可以是 sync 或 async。返值:
    - 真值 → 返该值(可能非 True,比如 row count)
    - 超时 → 返 None(调用方判 None = 超时)
    """
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(step)
