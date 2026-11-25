"""S3 协议实现 — `aioboto3`。

兼容 AWS S3 / MinIO / 阿里 OSS(均走 S3 协议)。

- endpoint_url:对外地址(MinIO/OSS 时显式指定)
- bucket:目标桶(启动时若不存在则尝试创建)
- key:对象 key(应用层自己定义,本类不强制)

2026-11-25 #19:client 复用 — 旧实现每个操作 `async with self._session.
client(...)` 新建 / 销毁 client(TLS 握手 + 连接池重建,高频操作浪费)。
现在懒初始化一个长生命周期 client,`close()` 统一释放。

2026-11-25 #18:exists/stat 区分「对象不存在」与「其他错误」(见各方法
docstring)。

2026-11-25 #27:`put_stream` — 临时文件 + upload_fileobj(multipart),
内存 O(chunk)。
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from io import BytesIO
from typing import Any, AsyncIterator, BinaryIO

import aioboto3
from botocore.exceptions import BotoCoreError, ClientError

from tgmonitor.core.objectstore.base import ObjectMeta, ObjectStore

log = logging.getLogger(__name__)

# 2026-11-25 P1-10:S3 "对象不存在"错误码 → 转抛 KeyError(key)。
# AWS get_object 缺 key 抛 NoSuchKey;部分兼容实现(MinIO / OSS / mock)抛
# NotFound 或 "404"(botocore 对无 body 响应用 HTTP 状态码兜底)。
_MISSING_KEY_CODES = frozenset({"NoSuchKey", "NotFound", "404"})


def _is_missing_key(exc: ClientError) -> bool:
    """ClientError 是否为「对象不存在」— 是则调用方应转抛 KeyError。"""
    return exc.response.get("Error", {}).get("Code") in _MISSING_KEY_CODES


# 2026-11-25 #19:put 走 multipart 的阈值。单次 PUT 有 5GB 上限(超了直接
# 失败),> 8MB 的媒体(照片 ~3MB、视频常 >8MB)基本都会走 multipart 分段,
# 兼得断点重试与 >5GB 支持。测试可 monkeypatch 此常量用小数据触发。
_MULTIPART_THRESHOLD = 8 * 1024 * 1024


class S3ObjectStore(ObjectStore):
    """S3 协议后端 — AWS S3 / MinIO / 阿里 OSS。

    用 `aioboto3` 异步客户端;`connect()` 时探测桶是否存在,不在则尝试创建。
    client 懒初始化 + 长生命周期复用(#19),`close()` 统一释放。
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
        # 长生命周期 client(#19):`ClientCreatorContext` 与进入后拿到的
        # 真正 client。`__aexit__` 会调 `client.__aexit__`(内部 close),
        # 是 aiobotocore 的标准释放路径。
        self._client_ctx: Any | None = None
        self._client: Any | None = None

    # ---- 生命周期 ----

    async def connect(self) -> None:
        """建 aioboto3 Session + 长生命周期 client + 探测 / 自动创建 bucket;失败上抛。

        2026-08-18 修:旧实现把 head_bucket / create_bucket 的所有异常吞掉,
        endpoint 填错 / 凭据错 / 网络不通 / 桶无权限在「保存设置」时完全感知
        不到,直到真正写 media(put_object)才报错(典型:
        `S3 API Requests must be made to API port`)。现在 connect() 做真实
        连通性校验,reconfigure 失败上抛 → 设置不落盘:
          - head_bucket 成功 → 桶存在且可达
          - ClientError 404 → 桶不存在,尝试 create_bucket("已存在"类视为成功)
          - 其他 ClientError / BotoCoreError → 原样上抛

        失败时释放已建的 client,不留半初始化连接。
        """
        self._session = aioboto3.Session()
        try:
            s3 = await self._get_client()
        except BaseException:
            self._session = None
            raise
        try:
            await s3.head_bucket(Bucket=self._bucket)
            return  # 桶存在且可达
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                await self._teardown_client()
                raise  # 403 无权限 / 400 端点错等 — 配置错误,上抛
        except BotoCoreError:
            await self._teardown_client()
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
                await self._teardown_client()
                raise  # 无建桶权限 / 端点错等 — 上抛
        except BotoCoreError:
            await self._teardown_client()
            raise

    async def close(self) -> None:
        """释放长生命周期 client(#19):`ClientCreatorContext.__aexit__`
        → `client.__aexit__`(内部 close 连接池),再丢弃 session 引用。
        """
        await self._teardown_client()
        self._session = None

    async def _teardown_client(self) -> None:
        ctx = self._client_ctx
        self._client_ctx = None
        self._client = None
        if ctx is not None:
            await ctx.__aexit__(None, None, None)

    async def _get_client(self) -> Any:
        """返回长生命周期 s3 client(懒初始化,#19)。

        `aioboto3.Session.client()` 返回的是 `ClientCreatorContext`(异步
        上下文管理器),必须进入后才能拿到可调用的 client —— 直接把 context
        对象当 client 用会导致所有 `put_object` / `head_bucket` 等调用报
        `'ClientCreatorContext' object has no attribute ...`。

        长生命周期:首次调用 `__aenter__` 创建并缓存;后续调用直接复用
        (省 TLS 握手 + 连接池重建);`close()` / connect 失败时 `__aexit__`
        释放。
        """
        if self._session is None:
            # v1.0.21:启动降级后 connect() 未成功时也会走到这里 — 用清晰的
            # RuntimeError 替代 assert(用户看到的是可操作提示,不是断言)。
            raise RuntimeError("对象存储未连接:connect() 未成功,请检查对象存储设置后重新保存")
        if self._client is None:
            ctx = self._session.client(
                "s3",
                endpoint_url=self._endpoint,
                region_name=self._region,
                aws_access_key_id=self._access,
                aws_secret_access_key=self._secret,
            )
            try:
                self._client = await ctx.__aenter__()
            except BaseException:
                await self._safe_exit_ctx(ctx)
                raise
            self._client_ctx = ctx
        return self._client

    @staticmethod
    async def _safe_exit_ctx(ctx: Any) -> None:
        """`__aenter__` 失败时兜底退出 context(幂等,失败不掩盖原异常)。"""
        try:
            await ctx.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001 — 释放失败不应掩盖真正的初始化错误
            pass

    # ---- 操作 ----

    async def put(self, key: str, data: bytes, meta: ObjectMeta | None = None) -> str:
        """写入对象;返回稳定 key。

        2026-11-25 #19:> 8MB(`_MULTIPART_THRESHOLD`)走 `upload_fileobj`
        multipart 分段 — 单次 PUT 有 5GB 上限,大对象必炸;multipart 无上限
        且分段可重试。小于阈值保持单次 PUT(一个请求,零额外开销)。

        契约不变:入参完整 bytes,内存仍 O(file)(data 已由调用方持有);
        真正 O(chunk) 的入口见 `put_stream`。

        meta.sha256:S3 后端不本地算哈希(multipart ETag 是分片 md5 混合,
        不等于 sha256),保持传入值 — 与旧行为一致。
        """
        async_op = len(data) > _MULTIPART_THRESHOLD
        extra_args = {"ContentType": meta.content_type} if meta and meta.content_type else None
        s3 = await self._get_client()
        if async_op:
            await s3.upload_fileobj(
                BytesIO(data),
                Bucket=self._bucket,
                Key=key,
                ExtraArgs=extra_args,
            )
        else:
            kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": key, "Body": data}
            if extra_args:
                kwargs.update(extra_args)
            await s3.put_object(**kwargs)
        return key

    async def put_stream(
        self,
        key: str,
        chunk_iter: AsyncIterator[bytes],
        meta: ObjectMeta | None = None,
    ) -> ObjectMeta:
        """流式写入(2026-11-25 #27)— 临时文件 + `upload_fileobj`(multipart)。

        取舍:`upload_fileobj` 只接受同步 file-like(内部线程池分段读取),
        不能直接消费 async iterator — 逐块收进 `NamedTemporaryFile` 再上传。
        内存峰值 O(chunk)(磁盘 O(file),与 FS 后端等价);比 `put(bytes)`
        的 O(file) 内存好,且天然获得 multipart 分段与断点重试。
        """
        s3 = await self._get_client()
        size = 0
        digest = hashlib.sha256()
        with tempfile.NamedTemporaryFile() as tf:
            async for chunk in chunk_iter:
                tf.write(chunk)
                size += len(chunk)
                digest.update(chunk)
            tf.flush()
            tf.seek(0)
            extra_args = {"ContentType": meta.content_type} if meta and meta.content_type else None
            await s3.upload_fileobj(
                tf,
                Bucket=self._bucket,
                Key=key,
                ExtraArgs=extra_args,
            )
        return ObjectMeta(
            content_type=meta.content_type if meta is not None else None,
            size=size,
            sha256=digest.hexdigest(),
        )

    async def get(self, key: str) -> bytes:
        """GET 全量 body(走 streaming read)。

        2026-11-25 P1-10:不存在 → 转抛 `KeyError(key)`,与 `base.py` 契约
        及 Local/Folder 后端对齐(此前让 aioboto3 抛 ClientError(NoSuchKey),
        ZipExporter 只兜 KeyError → 整包导出直接炸)。
        """
        s3 = await self._get_client()
        try:
            resp = await s3.get_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            if _is_missing_key(e):
                raise KeyError(key) from e
            raise
        async with resp["Body"] as stream:
            return await stream.read()

    async def exists(self, key: str) -> bool:
        """HEAD 探测;「对象不存在」(404 类)→ False,其他错误上抛。

        2026-11-25 #18:旧实现 `except Exception` 吞掉一切 — 网络抖动 /
        端点错 / 权限错全被解读为「不存在」,downloader 去重
        (`monitor/downloader.py:149` objectstore hit)失效 → 大文件重复下载。
        现在只有 ClientError 404 类(NoSuchKey / NotFound / 404,复用 get 的
        `_MISSING_KEY_CODES`)才 False;网络(BotoCoreError)/ 权限等其他
        错误 log.warning 后原样上抛。

        语义选择理由:base 契约只承诺「不存在 → False」;真错误静默降级成
        False 会掩盖故障(报告 #18 的原始事故)。上抛的调用方安全性已核实:
        monitor orchestrator(`orchestrator.py:200`)与 media 下载路径都兜
        `Exception` → ErrorOccurred,循环不会炸 — 得到明确失败而非假
        「未下载」。
        """
        s3 = await self._get_client()
        try:
            await s3.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as e:
            if _is_missing_key(e):
                return False
            log.warning("S3 head_object failed (key=%s): %s", key, e)
            raise
        except BotoCoreError as e:
            log.warning("S3 head_object network error (key=%s): %s", key, e)
            raise

    async def delete(self, key: str) -> None:
        """DELETE;不存在 idempotent 不抛。"""
        s3 = await self._get_client()
        await s3.delete_object(Bucket=self._bucket, Key=key)

    async def stat(self, key: str) -> ObjectMeta | None:
        """HEAD 拿 ContentType + ContentLength;不存在返 None,其他错误上抛。

        2026-11-25 #18:与 `exists` 同语义 — 404 类 → None(契约「不存在
        返 None(不抛)」只覆盖「不存在」);网络 / 权限等真错误 log.warning
        后上抛,不再静默当「无元数据」。
        """
        s3 = await self._get_client()
        try:
            h = await s3.head_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            if _is_missing_key(e):
                return None
            log.warning("S3 head_object failed (key=%s): %s", key, e)
            raise
        except BotoCoreError as e:
            log.warning("S3 head_object network error (key=%s): %s", key, e)
            raise
        return ObjectMeta(
            content_type=h.get("ContentType"),
            size=h.get("ContentLength"),
        )

    async def open_read(self, key: str) -> BinaryIO:
        """`BytesIO(await get())`;S3 streaming override 留给将来。"""
        return BytesIO(await self.get(key))

    async def stream_read(
        self,
        key: str,
        chunk_size: int = 65536,
    ) -> AsyncIterator[bytes]:
        """S3 真流式 — 2026-09-02 v1.5.2 PR #B6。

        用 boto3 原生 `iter_chunks(chunk_size)` async iterator — 内部走
        HTTP chunked transfer,内存峰值 O(chunk_size)。

        `chunk_size=64KB` 是经验值:Local 单次 read 64KB 在 SSD 上 ~150μs,
        async gen overhead 可忽略;S3 网络往返 ~50ms,与 chunk size 无关
        (boto3 内部按网络 buffer 大小切,64KB 是给 Local 的友好默认值)。

        2026-09-30 v1.10.x 兼容:部分 S3 兼容实现(早期 aioboto3 / OSS SDK /
        Mock 服务器)`resp["Body"]` 不直接提供 `iter_chunks`,而是裸 aiohttp
        `ClientResponse`(`content.iter_chunked(n)`)。分支处理:有 `iter_chunks`
        → 原生路径(零拷贝 + httpx/urllib 互转由 aiobotocore 内部做);
        否则用 `read(chunk_size)` 循环兜底(aiobotocore 任何版本都有 read)。

        2026-11-25 P1-10:失败语义与 `get()` 对齐 — 对象不存在 →
        `KeyError(key)`(此前 ClientError(NoSuchKey) 原样上抛,违反
        `base.py` 流式契约"失败语义与 get() 一致");其他错误原样上抛。
        """
        s3 = await self._get_client()
        try:
            resp = await s3.get_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            if _is_missing_key(e):
                raise KeyError(key) from e
            raise
        async with resp["Body"] as stream:
            if hasattr(stream, "iter_chunks"):
                async for chunk in stream.iter_chunks(chunk_size):
                    yield chunk
            else:
                # 兜底:aiobotocore StreamingBody 必有 read(chunk_size),
                # 语义等价 iter_chunks 但不开 generator,O(chunk_size)
                # 内存 + 略多的 event loop 调度。
                while True:
                    chunk = await stream.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk

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
        s3 = await self._get_client()
        paginator = s3.get_paginator("list_objects_v2")
        async for page in paginator.paginate(
            Bucket=self._bucket,
            Prefix=prefix,
        ):
            for obj in page.get("Contents", []) or []:
                key = obj.get("Key")
                if key:
                    yield key

    def uri(self, key: str) -> str | None:
        """P1-C3 能力:S3 无本地路径,但可给 `s3://bucket/key` URI 供复制。"""
        return f"s3://{self._bucket}/{key}"
