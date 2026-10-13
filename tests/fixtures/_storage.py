"""`storage` fixture + `InMemoryRepository` 测试替身 + `repo_backend` parametrize。

2026-08-31 v1.5.0 PR #A6:

- `InMemoryRepository` 留作 in-process 测试替身(等同语义,无 IO)
- `repo_backend` parametrize fixture 跑同一 case × N 后端;
  本 PR 仅注册 `inmemory`(同步 in-process 替身,无 IO),
  `postgres` / `mongo` 留 PR #A7(testcontainers 启动,docker 依赖)。

2026-10-12 plan-batch3 目标 3 step 3.1:JSONL 后端删,`repo_backend`
parametrize 移除 `"jsonl"` 元素;`JsonlFileStore` import 移除。
"""

from __future__ import annotations

import pytest_asyncio

from tests.fixtures._in_memory_repository import InMemoryRepository


@pytest_asyncio.fixture
async def storage() -> InMemoryRepository:
    """默认 storage fixture — 内存替身(行为一致,无 IO)。"""
    return InMemoryRepository()


# ---- repo_backend parametrize ----
# 设计:每个 parametrize 后端走一个真实实现,确保同一 case 在不同后端
# 行为一致(parity 测试矩阵)— 比单纯 InMemory 覆盖更广。
#
# 2026-10-12 plan-batch3 目标 3 step 3.1:JSONL 后端移除,`repo_backend`
# 暂只注册 `inmemory`(PR #A7 加 `postgres` / `mongo` 走 mongomock + testcontainers)。
@pytest_asyncio.fixture(params=["inmemory"])
async def repo_backend(request):
    """2026-10-12 plan-batch3 目标 3 step 3.1:仅 `inmemory` 后端(已移除 jsonl)。

    用法:
        async def test_xxx(repo_backend):
            await repo_backend.upsert_message(...)

    后续 PR #A7 加 `postgres` / `mongo`:`params=["inmemory", "postgres", "mongo"]`,
    fixture factory 加 docker 启动 / mongomock_motor 启动分支。
    """
    if request.param == "inmemory":
        return InMemoryRepository()
    # 后续 PR #A7 加 postgres / mongo 分支
    raise NotImplementedError(f"backend {request.param!r} not yet wired (PR #A7)")
