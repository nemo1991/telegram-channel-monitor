"""真 Postgres fixture(testcontainers,Docker required)— session 级 DSN。

2026-11-26 2.1.7:从 `tests/integration/conftest.py` 上提到 `tests/fixtures/`,
经 `tests/conftest.py` 注册到整个 tests 目录 —— 此前它只在 `tests/integration/`
下可见,`tests/test_storage_backends.py` 的 `seeded_repo` / `meta_seeded_repo`
想加 `pg` 参数也拿不到 DSN,PG parity 只能靠 integration 目录里另写一份用例。

设计要点:
- `pg_engine`:session-scope 真 PG(testcontainers 启动,跑完销毁),返 DSN。
  **惰性** —— 没有测试请求它就完全不起容器,默认离线跑零开销。
- `open_pg_repo(dsn)`:function 级「全新 schema」helper,`pg_repo` 与三后端
  parametrize 共用同一套建扩展 / 建 schema / 清表逻辑,不各写一份。
- 没有 Docker 时 `pg_engine` 自动 skip(DockerNotFoundError → pytest.skip):
  本地开发的善意兜底。**CI integration job 断言 skipped == 0**,防止 Docker
  没起来时整条 PG 验证静默消失(pytest 退出码仍为 0)。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tgmonitor.core.storage.postgres_repo import PostgresRepository

# `postgres_repo` 在模块顶层 import `asyncpg`(extra 依赖)。本模块经 pytest_plugins
# 对**整个** tests 目录生效,所以 `PostgresRepository` 一律函数内惰性 import ——
# 离线跑默认用例时,收集期不新增任何 import 依赖。

logger = logging.getLogger(__name__)


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
        pytest.skip("testcontainers[postgres] 未装", allow_module_level=False)
    try:
        return PostgresContainer("postgres:16-alpine")
    except Exception as e:  # noqa: BLE001 — DockerNotFoundError / ImageNotFoundError / 权限问题一律跳过
        pytest.skip(f"Docker 不可用,跳过 PG 集成测试: {type(e).__name__}: {e}")


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[str]:
    """启动 PostgresContainer,跑完 session 后销毁。

    返 DSN 字符串(asyncpg 只认 `postgresql://` 或 `postgres://`),使用方
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


async def open_pg_repo(dsn: str) -> PostgresRepository:
    """连上 session DSN 并返回一个**空 schema** 的仓库;用完由调用方 close()。

    2026-09-03 v1.6.0 PR #Q1:`CREATE EXTENSION pg_trgm` 需 superuser —
    testcontainers PG 默认 root 账号是 superuser,所以扩展能装。这里在
    repo.connect() 后、init_schema() 前显式 `CREATE EXTENSION` 一次
    (idempotent),保证 schema.sql 的 `CREATE EXTENSION IF NOT EXISTS`
    走过完整路径(包括 PermissionDenied 兜底)。

    随后 drop + 重建全部业务表,保证每个用例拿到的是全新 schema(同一 session
    DSN 被多个用例串行复用,直接 TRUNCATE 会漏掉序列 / 约束状态)。
    """
    from tgmonitor.core.storage.postgres_repo import PostgresRepository

    repo = PostgresRepository(dsn=dsn)
    await repo.connect()
    try:
        # 显式建扩展,确保后续 init_schema 的 GIN 索引能正常创建
        async with repo._pool.acquire() as conn:  # type: ignore[attr-defined]
            try:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            except Exception:  # noqa: BLE001 — 兜底:init_schema 内部也会再试,这里 best-effort
                pass
        await repo.init_schema()
        # 清理 schema:直接 drop tables + recreate,比 TRUNCATE 更快也更彻底
        async with repo._pool.acquire() as conn:  # type: ignore[attr-defined]
            await conn.execute("DROP TABLE IF EXISTS media, messages, channels, meta CASCADE")
        await repo.init_schema()
    except BaseException:
        await repo.close()
        raise
    return repo
