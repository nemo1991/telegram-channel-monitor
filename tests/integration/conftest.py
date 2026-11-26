"""集成测试 fixtures — 真 PG(testcontainers)。

提供:
- `pg_repo`:function-scope,基于 session 级 DSN 创 schema + 返
  `PostgresRepository` 实例(建扩展 / 建 schema / 清表逻辑在
  `tests/fixtures/_pg.py:open_pg_repo`,与三后端 parametrize 共用)。

设计要点:
- `pg_engine`(session-scope 真 PG DSN)2026-11-26 2.1.7 起上提到
  `tests/fixtures/_pg.py`,经 `tests/conftest.py` 的 `pytest_plugins` 对整个
  tests 目录生效 —— `tests/test_storage_backends.py` 的 `seeded_repo` /
  `meta_seeded_repo` 三后端 parametrize 与本目录共用同一个容器。
- PG 容器用 session-scope(启动慢,30-60s),function-scope 走 schema
  drop + recreate 隔离。
- 没有 Docker 时 `pg_engine` 自动 skip(DockerNotFoundError → pytest.skip),
  不会让本地 `pytest -m integration` 整链路炸;CI integration job 则断言
  skipped == 0。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest_asyncio

from tests.fixtures._pg import open_pg_repo

if TYPE_CHECKING:
    from tgmonitor.core.storage.postgres_repo import PostgresRepository


@pytest_asyncio.fixture
async def pg_repo(pg_engine) -> AsyncIterator[PostgresRepository]:
    """function-scope:每次测试全新 schema,避免 case 间状态泄漏。"""
    repo = await open_pg_repo(pg_engine)
    try:
        yield repo
    finally:
        await repo.close()
