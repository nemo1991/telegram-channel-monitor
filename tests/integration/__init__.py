"""集成测试包 — 2026-08-31 v1.5.0 PR #A7。

目的:覆盖真后端(Postgres / Mongo)的 parity 行为 —
`tests/test_storage_backends.py` 长期只覆盖 InMemory + JsonlFileStore 两
后端,PostgresRepository / MongoRepository 的 `list_media` /
`aggregate_per_channel` / `find_media_by_file_id` 真实路径只在生产
环境触发,**没有 contract 一致性保证**。

策略(asymmetry):
- **Postgres**:testcontainers 启动真 PG 服务(Docker required);跑真 SQL,
  验证 `schema.sql` / `init_schema()` / asyncpg JSON 序列化等真路径
- **Mongo**:mongomock_motor in-process mock(无需 Docker);验证基础 CRUD
  + `find_media_by_file_id` 本 PR bug fix。`$unwind` / `$match` aggregate
  pipeline mongomock 支持度参差,**`list_media` / `aggregate_per_channel`
  Mongo parity 不在本 PR 覆盖**(留 v1.5.1 真 Mongo via testcontainers 补)

Marker:全部测试 `@pytest.mark.integration`,默认 `addopts` 不开 ——
`uv run pytest -m integration` 显式启用。CI integration job 自动跑。
"""
