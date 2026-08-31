"""`bus` / `client` fixtures — 2026-08-31 v1.5.0 PR #A6。

原 tests/conftest.py::bus / ::client(行 424-431)。
"""

from __future__ import annotations

import pytest

from tgmonitor.core.events import EventBus
from tgmonitor.core.telegram.fake_client import FakeTelegramClient


@pytest.fixture
def bus() -> EventBus:
    """EventBus — 同步 fixture,各测试自管。"""
    return EventBus()


@pytest.fixture
def client() -> FakeTelegramClient:
    """FakeTelegramClient — 同步替身,无 IO,无 tdlib 依赖。"""
    return FakeTelegramClient()
