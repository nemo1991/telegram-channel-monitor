"""S3 协议实现 — `aioboto3`。

兼容 AWS S3 / MinIO / 阿里 OSS(均走 S3 协议)。

- endpoint_url:对外地址(MinIO/OSS 时显式指定)
- bucket:目标桶(启动时若不存在则尝试创建)
- key:对象 key(应用层自己定义,本类不强制)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, BinaryIO

import aioboto3

from tgmonitor.core.objectstore.base import ObjectMeta, ObjectStore


class S3ObjectStore(ObjectStore):
    backend_name = "s3"

    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._endpoint = endpoint_url
        self._region = region
        self._access = access_key
        self._secret = secret_key
        self._session: aioboto3.Session | None = None

    # ---- 生命周期 ----

    async def connect(self) -> None:
        self._session = aioboto3.Session()
        # 探测 / 自动建 bucket
        async with self._client() as s3:
            try:
                await s3.head_bucket(Bucket=self._bucket)
            except Exception:
                try:
                    if self._region == "us-east-1":
                        await s3.create_bucket(Bucket=self._bucket)
                    else:
                        await s3.create_bucket(
                            Bucket=self._bucket,
                            CreateBucketConfiguration={"LocationConstraint": self._region},
                        )
                except Exception:
                    # 已存在或其他原因(head 已报过) — 忽略
                    pass

    async def close(self) -> None:
        self._session = None

    @asynccontextmanager
    async def _client(self) -> Any:
        assert self._session is not None, "call connect() first"
        yield self._session.client(
            "s3",
            endpoint_url=self._endpoint,
            region_name=self._region,
            aws_access_key_id=self._access,
            aws_secret_access_key=self._secret,
        )

    # ---- 操作 ----

    async def put(self, key: str, data: bytes, meta: ObjectMeta | None = None) -> str:
        extra: dict[str, Any] = {}
        if meta and meta.content_type:
            extra["ContentType"] = meta.content_type
        async with self._client() as s3:
            await s3.put_object(Bucket=self._bucket, Key=key, Body=data, **extra)
        return key

    async def get(self, key: str) -> bytes:
        async with self._client() as s3:
            resp = await s3.get_object(Bucket=self._bucket, Key=key)
            async with resp["Body"] as stream:
                return await stream.read()

    async def exists(self, key: str) -> bool:
        try:
            async with self._client() as s3:
                await s3.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    async def delete(self, key: str) -> None:
        async with self._client() as s3:
            await s3.delete_object(Bucket=self._bucket, Key=key)

    async def stat(self, key: str) -> ObjectMeta | None:
        try:
            async with self._client() as s3:
                h = await s3.head_object(Bucket=self._bucket, Key=key)
            return ObjectMeta(
                content_type=h.get("ContentType"),
                size=h.get("ContentLength"),
            )
        except Exception:
            return None

    async def open_read(self, key: str) -> BinaryIO:
        from io import BytesIO

        return BytesIO(await self.get(key))
