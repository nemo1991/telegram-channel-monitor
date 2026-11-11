"""`storage` fixture — InMemoryRepository 测试替身。

InMemory 是 in-process 替身(等同语义,无 IO),作默认 storage fixture。
真实后端契约:SQLite 走 `tests/test_storage_backends.py` 等 parametrize
测试;Postgres 走 `tests/integration/`(testcontainers)。
"""

from __future__ import annotations

import pytest_asyncio

from tests.fixtures._in_memory_repository import InMemoryRepository


@pytest_asyncio.fixture
async def storage() -> InMemoryRepository:
    """默认 storage fixture — 内存替身(行为一致,无 IO)。"""
    return InMemoryRepository()