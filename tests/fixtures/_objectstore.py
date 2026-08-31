"""`objectstore` fixture — 2026-08-31 v1.5.0 PR #A6。

原 tests/conftest.py::objectstore(行 417-421)。
"""

from __future__ import annotations

import pytest_asyncio

from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.objectstore.local_store import LocalObjectStore


@pytest_asyncio.fixture
async def objectstore(tmp_path) -> ObjectStore:
    """LocalObjectStore(tests 用本地文件后端)— tmp_path 注入。"""
    s = LocalObjectStore(root=tmp_path / "media")
    await s.connect()
    return s
