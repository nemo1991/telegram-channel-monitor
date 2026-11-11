"""集成测试包 — 真 Postgres(testcontainers,Docker required)。

目的:覆盖 PostgresRepository 的真实 SQL 路径 — `schema.sql` /
`init_schema()` / asyncpg JSON 序列化 / `list_media` /
`aggregate_per_channel` / `find_media_by_file_id` 等只在真服务上才能
验证的行为。

v2.0.0:默认后端 SQLite 的契约由离线测试守门
(`tests/test_storage_backends.py` / `tests/test_sqlite_repo.py` /
`tests/test_thumbnail_repo_parity.py`,parametrize InMemory + SQLite);
Mongo 后端已删。本包只跑真 PG parity。

Marker:全部测试 `@pytest.mark.integration`,默认 `addopts` 不开 ——
`uv run pytest -m integration` 显式启用。CI integration job 自动跑。
"""