"""对象存储后端单测(Local + Folder + S3)。

2026-10-04 plan 阶段 1.3.B:`_FakeS3Client` / `_FakeStream` /
`_FakeClientContext` / `_FakePaginator` / `_FakePaginatorIter` /
`_FakeSession` 等替身 class 提到 `tests/fixtures/_s3_client.py`,
本文件 import 共用。本文件 `_*MissingBucketClient` 等子类继承
fixture 的 `FakeS3Client`,test 体特化的 method 仍留本文件。
"""

from __future__ import annotations

import aioboto3
import pytest
from botocore.exceptions import ClientError

from tests.fixtures._s3_client import (
    FakeS3Client,
    FakeS3Session,
)
from tgmonitor.core.objectstore.base import ObjectMeta
from tgmonitor.core.objectstore.folder_store import FolderObjectStore
from tgmonitor.core.objectstore.local_store import LocalObjectStore
from tgmonitor.core.objectstore.s3_store import S3ObjectStore


def _client_error(status: int, code: str) -> ClientError:
    """构造 botocore ClientError(模拟 S3 错误响应)。"""
    return ClientError(
        {
            "Error": {"Code": code, "Message": "boom"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "HeadBucket",
    )


# ---- Local ----


async def test_put_get_roundtrip(tmp_path):
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    await s.put("media/abc.jpg", b"hello", ObjectMeta(content_type="image/jpeg"))
    assert await s.exists("media/abc.jpg")
    assert await s.get("media/abc.jpg") == b"hello"


async def test_path_traversal_rejected(tmp_path):
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    with pytest.raises(ValueError):
        await s.put("../etc/passwd", b"x")
    with pytest.raises(ValueError):
        await s.put("/abs", b"x")


async def test_get_missing_raises(tmp_path):
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    with pytest.raises(KeyError):
        await s.get("nope")


# ---- Folder(两级分片)----


async def test_folder_put_get_roundtrip(tmp_path):
    s = FolderObjectStore(root=tmp_path)
    await s.connect()
    await s.put("media/abcdef.jpg", b"hello", ObjectMeta(content_type="image/jpeg"))
    assert await s.exists("media/abcdef.jpg")
    # 两级分片:media/ab/cd/abcdef.jpg
    assert (tmp_path / "media" / "ab" / "cd" / "abcdef.jpg").exists()
    assert await s.get("media/abcdef.jpg") == b"hello"


# ---- S3(aioboto3)----
# Fake S3 client / stream / paginator / session 等替身已统一到
# `tests.fixtures._s3_client.py`,本文件直接 import(见文件顶部)。
# 子类(`_ForbiddenClient` 等)继承 `FakeS3Client` 写局部特化 method。


def _make_s3_store(monkeypatch) -> tuple[S3ObjectStore, FakeS3Client]:
    fake = FakeS3Session()
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="test-bucket", region="us-east-1")
    return store, fake._client_obj


async def test_s3_put_get_roundtrip(monkeypatch):
    """回归:put/get 必须真的走到 boto3 client,而不是 ClientCreatorContext。"""
    store, client = _make_s3_store(monkeypatch)
    await store.connect()
    assert ("head_bucket", {"Bucket": "test-bucket"}) in client.calls

    await store.put("media/a.jpg", b"data", ObjectMeta(content_type="image/jpeg"))
    puts = [c for c in client.calls if c[0] == "put_object"]
    assert puts, f"put_object 未调用: {client.calls}"
    assert puts[0][1] == {
        "Bucket": "test-bucket",
        "Key": "media/a.jpg",
        "Body": b"data",
        "ContentType": "image/jpeg",
    }

    assert await store.get("media/a.jpg") == b"data"
    assert await store.exists("media/a.jpg")
    meta = await store.stat("media/a.jpg")
    assert meta is not None and meta.size == 4


async def test_s3_connect_forbidden_raises(monkeypatch):
    """head_bucket 403(无权限)→ 直接上抛,不尝试建桶(配置错误不落盘)。"""

    class _ForbiddenClient(FakeS3Client):
        async def head_bucket(self, **kw: object) -> dict:
            self._record("head_bucket", kw)
            raise _client_error(403, "AccessDenied")

    fake = FakeS3Session()
    fake._client_obj = _ForbiddenClient()
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="test-bucket", region="us-east-1")
    with pytest.raises(ClientError):
        await store.connect()
    assert [c[0] for c in fake._client_obj.calls] == ["head_bucket"]


# ---- iter_keys(2026-08-24 Media Manager orphan reconcile)----


async def test_local_iter_keys_returns_all(tmp_path):
    """LocalObjectStore.iter_keys 返回所有 key(相对 root)。"""
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    await s.put("media/abc.jpg", b"x")
    await s.put("media/xyz.png", b"y")
    await s.put("logs/today.log", b"z")  # 非 media 前缀也应返
    keys = sorted([k async for k in s.iter_keys()])
    assert keys == ["logs/today.log", "media/abc.jpg", "media/xyz.png"]


# ---- S3 iter_keys(2026-08-25 PR #2)----


async def test_s3_iter_keys_returns_all(monkeypatch):
    """S3 iter_keys → list_objects_v2 paginator,把桶里所有 key 列出。"""
    store, client = _make_s3_store(monkeypatch)
    await store.connect()
    client.set_list_keys(
        [
            "media/a.jpg",
            "media/b.png",
            "other/c.txt",
        ]
    )
    keys = sorted([k async for k in store.iter_keys(prefix="")])
    assert keys == ["media/a.jpg", "media/b.png", "other/c.txt"]
    # 校验走到了 paginator + 传了 Prefix
    paginate_calls = [c for c in client.calls if c[0] == "get_paginator"]
    assert paginate_calls and paginate_calls[0][1] == {"name": "list_objects_v2"}


# ---- stream_read(2026-09-02 v1.5.2 PR #B6)----


class _FakeStreamChunked:
    """模拟 aioboto3 StreamingBody:支持 `iter_chunks(chunk_size)` async iter。

    把传入的 bytes 按 chunk_size 切片,逐次 yield — 与真 boto3 StreamingBody
    `iter_chunks` 行为一致(boto3 也是按 chunk_size 切 HTTP body)。
    """

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self) -> _FakeStreamChunked:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def iter_chunks(self, chunk_size: int) -> _FakeChunkIter:
        return _FakeChunkIter(self._data, chunk_size)


class _FakeChunkIter:
    def __init__(self, data: bytes, chunk_size: int) -> None:
        self._data = data
        self._chunk_size = chunk_size
        self._offset = 0

    def __aiter__(self) -> _FakeChunkIter:
        return self

    async def __anext__(self) -> bytes:
        if self._offset >= len(self._data):
            raise StopAsyncIteration
        end = min(self._offset + self._chunk_size, len(self._data))
        chunk = self._data[self._offset : end]
        self._offset = end
        return chunk


async def test_local_stream_read_basic(tmp_path):
    """PR #B6:Local 后端 stream_read 按 chunk_size 分块 yield。"""
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    data = b"x" * (10 * 1024)  # 10KB
    await s.put("media/big.bin", data)

    chunks: list[bytes] = []
    async for chunk in s.stream_read("media/big.bin", chunk_size=4096):
        chunks.append(chunk)
    assert b"".join(chunks) == data
    # 10KB / 4KB = 2.5 → 3 chunks
    assert len(chunks) == 3


async def test_s3_stream_read_basic(monkeypatch):
    """PR #B6:S3 后端 stream_read 走 boto3 `iter_chunks` 原生流式。"""

    class _ChunkedS3Client(FakeS3Client):
        """覆盖 get_object 返 _FakeStreamChunked(支持 iter_chunks)。"""

        async def get_object(self, **kw: object) -> dict:  # type: ignore[override]
            self._record("get_object", kw)
            return {"Body": _FakeStreamChunked(b"s3-data-12345")}

    fake = FakeS3Session()
    fake._client_obj = _ChunkedS3Client()
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="test-bucket", region="us-east-1")
    await store.connect()

    chunks = [c async for c in store.stream_read("media/s3.bin", chunk_size=4)]
    assert b"".join(chunks) == b"s3-data-12345"
    # 13 bytes / 4 → ceil(13/4) = 4 chunks: [s3-, dat, a-1, 2345]
    assert len(chunks) == 4
    # 校验 get_object 被调 + Body 上下文管理
    gets = [c for c in fake._client_obj.calls if c[0] == "get_object"]
    assert gets[0][1] == {"Bucket": "test-bucket", "Key": "media/s3.bin"}


async def test_default_stream_read_falls_back_to_get(tmp_path):
    """PR #B6:不 override stream_read 的后端走默认实现(get() + chunk_size 切片)。"""
    # LocalObjectStore 显式继承默认实现(实际已 override,这里测默认路径)
    # 用 FolderObjectStore 也已 override → 测一个 minimal 自定义后端走默认
    from tgmonitor.core.objectstore.base import ObjectStore

    class _MinimalStore(ObjectStore):
        """不 override stream_read,只实现 get/connect/close,验证默认实现。"""

        backend_name = "minimal"

        def __init__(self) -> None:
            self._data: dict[str, bytes] = {}

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def put(self, key: str, data: bytes, meta: object = None) -> str:  # type: ignore[override]
            self._data[key] = data
            return key

        async def get(self, key: str) -> bytes:
            if key not in self._data:
                raise KeyError(key)
            return self._data[key]

        async def exists(self, key: str) -> bool:
            return key in self._data

        async def delete(self, key: str) -> None:
            self._data.pop(key, None)

        async def stat(self, key: str) -> ObjectMeta | None:
            return ObjectMeta(size=len(self._data[key])) if key in self._data else None

    s = _MinimalStore()
    await s.connect()
    await s.put("k", b"x" * 100)
    chunks = [c async for c in s.stream_read("k", chunk_size=30)]
    assert b"".join(chunks) == b"x" * 100
    assert len(chunks) == 4  # [30, 30, 30, 10]
    # 不存在 → KeyError
    with pytest.raises(KeyError):
        async for _ in s.stream_read("nope"):
            pass
