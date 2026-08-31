"""集成测试 fixtures — 2026-08-31 v1.5.0 PR #A7。

提供:
- `pg_engine`:session-scope 真 PG(testcontainers 启动,跑完销毁)
- `pg_repo`:function-scope,基于 `pg_engine` 创 schema + 返
  `PostgresRepository` 实例
- `mongo_client`:session-scope mongomock_motor in-process client
- `mongo_repo`:function-scope,基于 `mongo_client` 返 `MongoRepository` 实例

设计要点:
- PG fixture 用 session-scope(启动 PG 容器慢,30-60s),function-scope
  走 schema drop + recreate 隔离
- Mongo fixture 全 function-scope(mongomock_motor in-memory,cheap)
- 没有 Docker 时 `pg_engine` 自动 skip(DockerNotFoundError → pytest.skip),
  不会让 `pytest -m integration` 整链路炸
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

from tgmonitor.core.storage.mongo_repo import MongoRepository
from tgmonitor.core.storage.postgres_repo import PostgresRepository

logger = logging.getLogger(__name__)


# ---- Postgres (testcontainers) ----


def _pg_container_or_skip():
    """起 PostgresContainer;无 Docker → skip 整 PG fixture 模块。

    2026-08-31 v1.5.0 PR #A7 修复:`testcontainers>=4` 把 `PostgresContainer`
    从顶层 `testcontainers.postgres` 搬到 `testcontainers.community.postgres`,
    顶层路径仍兼容但 DeprecationWarning 噪声大;先试 community 路径,
    fallback 顶层路径(老版 testcontainers 没 community 子包)。
    """
    import warnings

    try:
        try:
            from testcontainers.community.postgres import PostgresContainer
        except ImportError:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers[postgresql] 未装", allow_module_level=False)
    try:
        return PostgresContainer("postgres:16-alpine")
    except Exception as e:  # DockerNotFoundError / ImageNotFoundError / 权限问题
        pytest.skip(f"Docker 不可用,跳过 PG 集成测试: {type(e).__name__}: {e}")


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[str]:
    """启动 PostgresContainer,跑完 session 后销毁。

    返 DSN 字符串(asyncpg 只认 `postgresql://` 或 `postgres://`),`pg_repo`
    拿去造 PostgresRepository 实例。

    2026-08-31 v1.5.0 PR #A7 修复:testcontainers 4.x 默认返
    `postgresql+psycopg2://`(SQLAlchemy 风格 DSN),asyncpg 只接受 plain
    `postgresql://` — 需要剥离 `+psycopg2` 后缀。
    """
    container = _pg_container_or_skip()
    container.start()
    try:
        sync_dsn = container.get_connection_url()
        # 兼容多种 scheme:testcontainers 4.x 默认 `postgresql+psycopg2`,
        # 老版本可能 `postgresql`,偶有 `postgres`。统一剥成 plain asyncpg scheme。
        async_dsn = re.sub(r"^(postgres(?:ql)?)\+\w+://", r"\1://", sync_dsn, count=1)
        if async_dsn == sync_dsn and not sync_dsn.startswith(("postgresql://", "postgres://")):
            # 仍未匹配 — 强插 scheme
            async_dsn = "postgresql://" + sync_dsn.split("://", 1)[-1]
        yield async_dsn
    finally:
        container.stop()


@pytest_asyncio.fixture
async def pg_repo(pg_engine) -> AsyncIterator[PostgresRepository]:
    """function-scope:每次测试全新 schema,避免 case 间状态泄漏。"""
    repo = PostgresRepository(dsn=pg_engine)
    await repo.connect()
    try:
        await repo.init_schema()
        # 清理 schema:直接 drop tables + recreate,比 TRUNCATE 更快也更彻底
        async with repo._pool.acquire() as conn:  # type: ignore[attr-defined]
            await conn.execute("DROP TABLE IF EXISTS media, messages, channels, meta CASCADE")
        await repo.init_schema()
        yield repo
    finally:
        await repo.close()


# ---- Mongo (mongomock_motor) ----


@pytest.fixture(scope="session")
def mongo_client() -> Iterator:
    """session-scope mongomock_motor AsyncIOMotorClient。

    mongomock_motor 实际是 in-process dict,**不走真 Mongo**,所以没必要
    session-scope(function-scope 也 fast)—— 但保持 session-scope 与 PG
    一致,future 切真 testcontainers[mongodb] 时改一处即可。
    """
    try:
        from mongomock_motor import AsyncMongoMockClient
    except ImportError:
        pytest.skip("mongomock_motor 未装", allow_module_level=False)
    client = AsyncMongoMockClient()
    yield client


@pytest_asyncio.fixture
async def mongo_repo(mongo_client) -> AsyncIterator[MongoRepository]:
    """function-scope:每次测试全新 db,避免 case 间状态泄漏。

    mongomock_motor 没有真 db drop,用 distinct db_name(`test_<uuid>`)
    天然隔离;每个 case 拿一个新名字。
    """
    import uuid

    db_name = f"test_{uuid.uuid4().hex[:8]}"
    repo = MongoRepository.from_client(mongo_client, db_name)
    await repo.init_schema()
    try:
        yield repo
    finally:
        # from_client 不关 client(mongomock 无 close),无需清理
        pass
