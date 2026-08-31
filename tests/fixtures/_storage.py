"""`storage` fixture + `InMemoryRepository` 测试替身 + `repo_backend` parametrize。

2026-08-31 v1.5.0 PR #A6:

- `InMemoryRepository` 留作 in-process 测试替身(等同语义,无 IO)
- `repo_backend` parametrize fixture 跑同一 case × N 后端;
  本 PR 仅注册 `inmemory` + `jsonl`(同步文件持久化,真覆盖磁盘读写),
  `postgres` / `mongo` 留 PR #A7(testcontainers 启动,docker 依赖)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio

from tests.fixtures._in_memory_repository import InMemoryRepository
from tgmonitor.core.storage.jsonl_store import JsonlFileStore
from tgmonitor.core.storage.repository import StorageRepository


@pytest_asyncio.fixture
async def storage() -> InMemoryRepository:
    """默认 storage fixture — 内存替身(行为一致,无 IO)。"""
    return InMemoryRepository()


# ---- repo_backend parametrize ----
# 设计:每个 parametrize 后端走一个真实实现,确保同一 case 在不同后端
# 行为一致(parity 测试矩阵)— 比单纯 InMemory 覆盖更广。
#
# 注意:fixture 用 `tmp_path_factory` 而非 `tmp_path`(后者是 function-scope,
# parametrize 需要 session/module 共享)— 这里走 function-scope 但每次
# case 自带独立 tmp_path,避免泄漏。
@pytest_asyncio.fixture(params=["inmemory", "jsonl"])
async def repo_backend(request, tmp_path) -> AsyncIterator[StorageRepository]:
    """2026-08-31 v1.5.0 PR #A6:4 后端 parametrize — 当前活跃 inmemory + jsonl。

    用法:
        async def test_xxx(repo_backend):
            await repo_backend.upsert_message(...)

    后续 PR #A7 加 `postgres` / `mongo`:`params=["inmemory", "jsonl",
    "postgres", "mongo"]`,fixture factory 加 docker 启动分支。
    """
    backend = request.param
    if backend == "inmemory":
        yield InMemoryRepository()
    elif backend == "jsonl":
        store = JsonlFileStore(root=tmp_path / "jsonl")
        await store.connect()
        try:
            yield store
        finally:
            await store.close()
    else:
        # 后续 PR #A7 加 postgres / mongo 分支
        raise NotImplementedError(f"backend {backend!r} not yet wired (PR #A7)")
