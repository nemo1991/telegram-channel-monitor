"""StorageRepository 工厂 — 根据 config 装配 Postgres 或 Mongo。

实现类**懒加载**:只 import 用户实际选中的那个,避免装 Postgres 时被强制拉 motor(或反过来)。

2026-10-12 plan-batch3 目标 3 step 3.1:删除 JSONL 后端支持。`build_storage`
只接受 POSTGRES / MONGO,若 settings 仍带 `DBBackend.JSONL` 启动期
`DBBackend.JSONL` 已被 `config.py` 校验拦截(详见 `Settings.validate` 改动),
此处兜底 `raise ValueError`。
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
        f"(plan-batch3 目标 3 后已不再支持 JSONL,请改 postgres 或 mongo)"
    )
