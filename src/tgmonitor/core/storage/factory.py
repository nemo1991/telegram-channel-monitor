"""StorageRepository 工厂 — 根据 config 装配 Postgres 或 Mongo。

实现类**懒加载**:只 import 用户实际选中的那个,避免装 Postgres 时被强制拉 motor(或反过来)。
"""
from __future__ import annotations

from tgmonitor.core.config import DBBackend, Settings
from tgmonitor.core.storage.repository import StorageRepository


def build_storage(settings: Settings) -> StorageRepository:
    if settings.db_backend == DBBackend.POSTGRES:
        from tgmonitor.core.storage.postgres_repo import PostgresRepository

        return PostgresRepository(dsn=settings.db_dsn)
    if settings.db_backend == DBBackend.MONGO:
        from tgmonitor.core.storage.mongo_repo import MongoRepository

        return MongoRepository(dsn=settings.db_dsn, database="tgmonitor")
    if settings.db_backend == DBBackend.JSONL:
        from tgmonitor.core.storage.jsonl_store import JsonlFileStore

        return JsonlFileStore(root=settings.db_root)
    raise ValueError(f"unknown db backend: {settings.db_backend}")

