"""`monitor` / `app` fixtures — 2026-08-31 v1.5.0 PR #A6。

原 tests/conftest.py::monitor / ::app(行 434-442)。

依赖 `bus` / `client` / `storage` / `objectstore` / `settings` —
5 个 fixture 全是 function-scope,pytest 自动按依赖图注入。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio

from tgmonitor.core.app_service import AppService
from tgmonitor.core.monitor.service import MonitorService


@pytest_asyncio.fixture
async def monitor(bus, client, storage, objectstore, settings) -> MonitorService:
    """MonitorService — 监听 / 同步 / 增量更新等测试需要。"""
    return MonitorService(bus, client, storage, objectstore, settings)


@pytest_asyncio.fixture
async def app(bus, client, storage, objectstore, settings) -> AsyncIterator[AppService]:
    """AppService facade — UI / 集成测试入口。无 teardown(子服务状态自管)。"""
    svc = AppService(bus, client, storage, objectstore, settings)
    yield svc
