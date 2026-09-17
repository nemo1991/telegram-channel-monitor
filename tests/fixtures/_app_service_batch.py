"""`tests/test_app_service_batch_*.py` 共享 fixtures — 2026-09-17 PR 4 mega-split。

原 `tests/test_app_service_batch.py`(690 LOC)→ 6 个按 concern 拆分的文件 +
1 个元数据文件,共享 fixtures 集中到本模块,避免 7 个文件各复制 50 行 fixture 代码。

模式:AsyncMock 注入 client / storage / objects(单元测速),EventBus 真订阅
(验证 emit 序列)。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tgmonitor.core.app_service import AppService
from tgmonitor.core.events import (
    BatchDone,
    BatchProgress,
    EventBus,
    MessageEdited,
)


@pytest.fixture
def bus() -> EventBus:
    """最小 EventBus — function-scope,每个 test 一个新实例。"""
    return EventBus()


@pytest.fixture
async def app(bus: EventBus) -> AppService:
    """最小可用的 AppService — AsyncMock client / storage / objects。

    默认所有 RPC 成功(无 exception,无 side_effect)。
    `_is_paused = False`(未暂停)— paused 测试用 `paused_app` fixture。
    """
    client = AsyncMock()
    client.state = "phone_required"
    storage = AsyncMock()
    objects = AsyncMock()
    objects.backend_name = "fake"
    settings = MagicMock()
    settings.media_policy = "full"
    svc = AppService(bus, client, storage, objects, settings)
    svc._is_paused = False  # type: ignore[attr-defined]
    return svc


@pytest.fixture
async def paused_app(bus: EventBus) -> AppService:
    """暂停态 AppService — paused guard 走 short-circuit,不调 client。"""
    client = AsyncMock()
    client.state = "phone_required"
    storage = AsyncMock()
    objects = AsyncMock()
    objects.backend_name = "fake"
    settings = MagicMock()
    settings.media_policy = "full"
    svc = AppService(bus, client, storage, objects, settings)
    svc._is_paused = True  # type: ignore[attr-defined]
    return svc


@pytest.fixture
def collected(bus: EventBus) -> list:
    """订阅 BatchProgress + BatchDone + MessageEdited,事件 append 到 list。"""
    out: list = []

    async def _save(evt: object) -> None:
        out.append(evt)

    bus.subscribe(BatchProgress, _save)
    bus.subscribe(BatchDone, _save)
    bus.subscribe(MessageEdited, _save)
    return out


def bus_subscribe_app(app: AppService, evt_type, fn) -> None:  # type: ignore[no-untyped-def]
    """helper:订阅 app.bus 上的事件(供 inline 定义使用)。"""
    app.bus.subscribe(evt_type, fn)


async def _make_app(bus: EventBus, *, client_state: str = "phone_required") -> AppService:
    """构造最小 AppService,client state 可定制(供 state-machine 直测)。

    与 `app` fixture 区别:fixture 返回 svc;helper 供 inline 调用,不要 fixture
    注入因 client_state 想传不同值。
    """
    from tgmonitor.core.telegram.fake_client import FakeTelegramClient

    client = FakeTelegramClient()
    client._state = client_state
    storage = AsyncMock()
    objects = AsyncMock()
    objects.backend_name = "fake"
    settings = MagicMock()
    settings.media_policy = "full"
    svc = AppService(bus, client, storage, objects, settings)
    svc._is_paused = False  # type: ignore[attr-defined]
    return svc


async def _make_unconfigured_app(bus: EventBus) -> AppService:
    """构造用 UnconfiguredClient 的 AppService(无凭据,bootstrap 应返 phone_required)。

    测 AppService.bootstrap 与 UnconfiguredClient 协同:不调 TDLib,仅返引导文案。
    """
    from tgmonitor.core.telegram.unconfigured import UnconfiguredTelegramClient

    client = UnconfiguredTelegramClient()
    storage = AsyncMock()
    objects = AsyncMock()
    objects.backend_name = "fake"
    settings = MagicMock()
    settings.media_policy = "full"
    svc = AppService(bus, client, storage, objects, settings)
    svc._is_paused = False  # type: ignore[attr-defined]
    return svc
