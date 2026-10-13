"""StorageRepository 工厂 — 根据 config 装配 Postgres 或 Mongo。

实现类**懒加载**:只 import 用户实际选中的那个,避免装 Postgres 时被强制拉 motor(或反过来)。

`build_storage` 只接受 POSTGRES / MONGO;`config.py` 校验阶段会拒绝
`DBBackend.JSONL` 等未知值,此处兜底 `raise ValueError`。
"""

from __future__ import annotations

from tgmonitor.core.config import DBBackend, Settings
from tgmonitor.core.storage.repository import StorageRepository


def build_storage(settings: Settings) -> StorageRepository:
    """根据 settings.db_backend 选 postgres / mongo 后端(懒加载对应模块)。"""
    if settings.db_backend == DBBackend.POSTGRES:
        from tgmonitor.core.storage.postgres_repo import PostgresRepository

        return PostgresRepository(dsn=settings.db_dsn)
    if settings.db_backend == DBBackend.MONGO:
        from tgmonitor.core.storage.mongo_repo import MongoRepository

        return MongoRepository(dsn=settings.db_dsn, database="tgmonitor")
    raise ValueError(
        f"unknown db backend: {settings.db_backend!r} "
        f"(仅支持 postgres 或 mongo)"
    )
