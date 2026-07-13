"""ObjectStore 工厂 — 根据 config 装配具体后端。

实现类**懒加载**:只 import 用户实际选中的那个,避免装 Local 时被强制拉 aioboto3
(或装 S3 时被强制拉文件存储依赖等)。
"""
from __future__ import annotations

from tgmonitor.core.config import ObjectStoreBackend, Settings
from tgmonitor.core.objectstore.base import ObjectStore


def build_object_store(settings: Settings) -> ObjectStore:
    """根据 settings.objectstore_backend 选 local / s3。"""
    if settings.objectstore_backend == ObjectStoreBackend.LOCAL:
        from tgmonitor.core.objectstore.local_store import LocalObjectStore

        return LocalObjectStore(root=settings.objectstore_root)
    if settings.objectstore_backend == ObjectStoreBackend.FOLDER:
        from tgmonitor.core.objectstore.folder_store import FolderObjectStore

        return FolderObjectStore(root=settings.objectstore_root)
    if settings.objectstore_backend == ObjectStoreBackend.S3:
        from tgmonitor.core.objectstore.s3_store import S3ObjectStore

        return S3ObjectStore(
            bucket=settings.objectstore_bucket,
            endpoint_url=settings.objectstore_endpoint,
            region=settings.objectstore_region,
            access_key=settings.objectstore_access_key,
            secret_key=settings.objectstore_secret_key,
        )
    raise ValueError(f"unknown object store backend: {settings.objectstore_backend}")

