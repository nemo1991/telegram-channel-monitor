"""S3 协议实现 — `aioboto3`。

兼容 AWS S3 / MinIO / 阿里 OSS(均走 S3 协议)。

- endpoint_url:对外地址(MinIO/OSS 时显式指定)
- bucket:目标桶(启动时若不存在则尝试创建)
- key:对象 key(应用层自己定义,本类不强制)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, BinaryIO

import aioboto3
from botocore.exceptions import BotoCoreError, ClientError

from tgmonitor.core.objectstore.base import ObjectMeta, ObjectStore

log = logging.getLogger(__name__)


class S3ObjectStore(ObjectStore):
    """S3 协议后端 — AWS S3 / MinIO / 阿里 OSS。

    用 `aioboto3` 异步客户端;`connect()` 时探测桶是否存在,不在则尝试创建。
    """

    backend_name = "s3"

    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        """`endpoint_url` = MinIO / OSS 时显式指定;credentials 走 env 或显式传。"""
        self._bucket = bucket
        self._endpoint = endpoint_url
        self._region = region
        self._access = access_key
        self._secret = secret_key
        self._session: aioboto3.Session | None = None

    # ---- 生命周期 ----

    async def connect(self) -> None:
        """建 aioboto3 Session + 探测 / 自动创建 bucket;失败上抛。

        2026-08-18 修:旧实现把 head_bucket / create_bucket 的所有异常吞掉,
        endpoint 填错 / 凭据错 / 网络不通 / 桶无权限在「保存设置」时完全感知
        不到,直到真正写 media(put_object)才报错(典型:
        `S3 API Requests must be made to API port`)。现在 connect() 做真实
        连通性校验,reconfigure 失败上抛 → 设置不落盘:
          - head_bucket 成功 → 桶存在且可达
          - ClientError 404 → 桶不存在,尝试 create_bucket("已存在"类视为成功)
          - 其他 ClientError / BotoCoreError → 原样上抛
        """
        self._session = aioboto3.Session()
        async with self._client() as s3:
            try:
                await s3.head_bucket(Bucket=self._bucket)
                return  # 桶存在且可达
            except ClientError as exc:
                if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                    raise  # 403 无权限 / 400 端点错等 — 配置错误,上抛
            except BotoCoreError:
                raise  # 网络 / 端点 / 凭据类错误 — 上抛
            # 桶不存在(404)→ 尝试自动创建
            try:
                if self._region == "us-east-1":
                    await s3.create_bucket(Bucket=self._bucket)
                else:
                    await s3.create_bucket(
                        Bucket=self._bucket,
                        CreateBucketConfiguration={"LocationConstraint": self._region},
                    )
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                    raise  # 无建桶权限 / 端点错等 — 上抛
            except BotoCoreError:
                raise

    async def close(self) -> None:
        """释放 session 引用(aioboto3 内部 client 在 GC 时关)。"""
        self._session = None

    @asynccontextmanager
    async def _client(self) -> Any:
        """yield 真正的 boto3 client。

        `aioboto3.Session.client()` 返回的是 `ClientCreatorContext`(异步
        上下文管理器),必须先 `async with` 进入才能拿到可调用的 client ——
        直接 yield context 对象会导致所有 `put_object` / `head_bucket` 等
        调用报 `'ClientCreatorContext' object has no attribute ...`。
        """
        if self._session is None:
            # v1.0.21:启动降级后 connect() 未成功时也会走到这里 — 用清晰的
            # RuntimeError 替代 assert(用户看到的是可操作提示,不是断言)。
            raise RuntimeError("对象存储未连接:connect() 未成功,请检查对象存储设置后重新保存")
        async with self._session.client(
            "s3",
            endpoint_url=self._endpoint,
            region_name=self._region,
            aws_access_key_id=self._access,
            aws_secret_access_key=self._secret,
        ) as s3:
            yield s3

    # ---- 操作 ----

    async def put(self, key: str, data: bytes, meta: ObjectMeta | None = None) -> str:
        """单次 PUT;若 meta.content_type 给了就带上 ContentType。"""
        extra: dict[str, Any] = {}
        if meta and meta.content_type:
            extra["ContentType"] = meta.content_type
        async with self._client() as s3:
            await s3.put_object(Bucket=self._bucket, Key=key, Body=data, **extra)
        return key

    async def get(self, key: str) -> bytes:
        """GET 全量 body(走 streaming read);不存在让 aioboto3 抛。"""
        async with self._client() as s3:
            resp = await s3.get_object(Bucket=self._bucket, Key=key)
            async with resp["Body"] as stream:
                return await stream.read()

    async def exists(self, key: str) -> bool:
        """HEAD 不存在 / 网络错误都返 False(best-effort)。"""
        try:
            async with self._client() as s3:
                await s3.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    async def delete(self, key: str) -> None:
        """DELETE;不存在 idempotent 不抛。"""
        async with self._client() as s3:
            await s3.delete_object(Bucket=self._bucket, Key=key)

    async def stat(self, key: str) -> ObjectMeta | None:
        """HEAD 拿 ContentType + ContentLength;不存在 / 错误返 None。"""
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
        """默认 `BytesIO(await get())`;S3 streaming override 留给将来。"""
        from io import BytesIO

        return BytesIO(await self.get(key))

    async def iter_keys(self, prefix: str = "") -> AsyncIterator[str]:
        """S3 后端 — 用 `list_objects_v2` paginator 枚举桶里所有 key(2026-08-25 PR #2)。

        实现要点:
        - 必须传 `prefix`(E2 设计)— 桶里通常有非 media 资产,全桶扫描慢且
          容易把无关文件当孤儿误删。`AppService.reconcile_orphans` 固定传
          `prefix="media/"`。
        - paginator 由 boto3 内置分页(默认每页 1000),`async for` 拿每个
          key。
        - 出错(`ClientError` / `BotoCoreError`)→ 上抛,调用方
          `AppService.reconcile_orphans` 已包 try/except。
        - `Prefix=""` 走全桶扫描,API 允许但代价高,文档警告用户。
        """
        if not prefix:
            log.warning(
                "S3.iter_keys called with empty prefix — full bucket scan, "
                "may be slow / costly on large buckets"
            )
        if self._session is None:
            raise RuntimeError("对象存储未连接:connect() 未成功,请检查对象存储设置后重新保存")
        async with self._client() as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(
                Bucket=self._bucket,
                Prefix=prefix,
            ):
                for obj in page.get("Contents", []) or []:
                    key = obj.get("Key")
                    if key:
                        yield key
