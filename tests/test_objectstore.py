"""对象存储后端单测(Local + Folder + S3)。"""
from __future__ import annotations

import asyncio

import aioboto3
import pytest

from tgmonitor.core.objectstore.base import ObjectMeta
from tgmonitor.core.objectstore.folder_store import FolderObjectStore
from tgmonitor.core.objectstore.local_store import LocalObjectStore
from tgmonitor.core.objectstore.s3_store import S3ObjectStore


async def _wrap_to_thread(monkeypatch, calls: list[str]) -> None:
    """把 asyncio.to_thread 包一层:记录被调度函数名,再照常执行。"""

    original = asyncio.to_thread

    async def recording(fn, *args, **kwargs):
        calls.append(getattr(fn, "__name__", repr(fn)))
        return await original(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", recording)


# ---- Local ----

async def test_put_get_roundtrip(tmp_path):
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    await s.put("media/abc.jpg", b"hello", ObjectMeta(content_type="image/jpeg"))
    assert await s.exists("media/abc.jpg")
    assert await s.get("media/abc.jpg") == b"hello"


async def test_put_get_uses_to_thread(tmp_path, monkeypatch):
    """写盘/读盘必须走 asyncio.to_thread,避免大文件 IO 阻塞事件循环。"""
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    calls: list[str] = []
    await _wrap_to_thread(monkeypatch, calls)
    data = b"x" * (1024 * 1024)  # 1MB
    await s.put("media/big.bin", data, ObjectMeta(content_type="application/octet-stream"))
    assert await s.get("media/big.bin") == data
    assert any("write" in c for c in calls), f"put 未走 to_thread: {calls}"
    assert "read_bytes" in calls, f"get 未走 to_thread: {calls}"


async def test_delete(tmp_path):
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    await s.put("k", b"x")
    await s.delete("k")
    assert not await s.exists("k")


async def test_path_traversal_rejected(tmp_path):
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    with pytest.raises(ValueError):
        await s.put("../etc/passwd", b"x")
    with pytest.raises(ValueError):
        await s.put("/abs", b"x")


async def test_stat(tmp_path):
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    await s.put("k", b"12345")
    meta = await s.stat("k")
    assert meta is not None
    assert meta.size == 5


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


async def test_folder_shard_zero_is_flat(tmp_path):
    s = FolderObjectStore(root=tmp_path, shard_size=0)
    await s.connect()
    await s.put("media/abcdef.jpg", b"hello")
    assert (tmp_path / "media" / "abcdef.jpg").exists()


async def test_folder_put_get_uses_to_thread(tmp_path, monkeypatch):
    """Folder 后端写盘/读盘同样必须走 asyncio.to_thread。"""
    s = FolderObjectStore(root=tmp_path)
    await s.connect()
    calls: list[str] = []
    await _wrap_to_thread(monkeypatch, calls)
    data = b"x" * (1024 * 1024)  # 1MB
    await s.put("media/big.bin", data)
    assert await s.get("media/big.bin") == data
    assert any("write" in c for c in calls), f"put 未走 to_thread: {calls}"
    assert "read_bytes" in calls, f"get 未走 to_thread: {calls}"


# ---- S3(aioboto3)----


class _FakeStream:
    """模拟 aioboto3 StreamingBody:异步上下文管理器 + read()。"""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def read(self) -> bytes:
        return self._data


class _FakeS3Client:
    """记录调用的假 boto3 client。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _record(self, op: str, kw: dict) -> None:
        self.calls.append((op, dict(kw)))

    async def head_bucket(self, **kw: object) -> dict:
        self._record("head_bucket", kw)
        return {}

    async def create_bucket(self, **kw: object) -> dict:
        self._record("create_bucket", kw)
        return {}

    async def put_object(self, **kw: object) -> dict:
        self._record("put_object", kw)
        return {"ETag": '"abc"'}

    async def get_object(self, **kw: object) -> dict:
        self._record("get_object", kw)
        return {"Body": _FakeStream(b"data")}

    async def head_object(self, **kw: object) -> dict:
        self._record("head_object", kw)
        return {"ContentType": "image/jpeg", "ContentLength": 4}

    async def delete_object(self, **kw: object) -> dict:
        self._record("delete_object", kw)
        return {}


class _FakeClientContext:
    """模拟 aioboto3 的 ClientCreatorContext。

    关键行为:`Session.client()` 返回的是**异步上下文管理器本身**,必须
    `async with` 进入后才能拿到真正的 client —— 修复前 s3_store 直接把
    context 对象当 client 用,`put_object` 等调用全部 AttributeError。
    """

    def __init__(self, client: _FakeS3Client) -> None:
        self._client = client

    async def __aenter__(self) -> _FakeS3Client:
        return self._client

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def __init__(self) -> None:
        self._fake_client = _FakeS3Client()

    def client(self, *args: object, **kw: object) -> _FakeClientContext:
        return _FakeClientContext(self._fake_client)


def _make_s3_store(monkeypatch) -> tuple[S3ObjectStore, _FakeS3Client]:
    fake = _FakeSession()
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="test-bucket", region="us-east-1")
    return store, fake._fake_client


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


async def test_s3_connect_creates_bucket_when_missing(monkeypatch):
    """head_bucket 抛异常(桶不存在)→ 走 create_bucket 分支。"""

    class _MissingBucketClient(_FakeS3Client):
        async def head_bucket(self, **kw: object) -> dict:
            self._record("head_bucket", kw)
            raise Exception("NoSuchBucket")

    fake = _FakeSession()
    fake._fake_client = _MissingBucketClient()
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="new-bucket", region="ap-east-1")
    await store.connect()
    created = [c for c in fake._fake_client.calls if c[0] == "create_bucket"]
    assert created, f"create_bucket 未调用: {fake._fake_client.calls}"
    assert created[0][1]["Bucket"] == "new-bucket"
    assert created[0][1]["CreateBucketConfiguration"]["LocationConstraint"] == "ap-east-1"


async def test_s3_delete(monkeypatch):
    store, client = _make_s3_store(monkeypatch)
    await store.connect()
    await store.delete("media/a.jpg")
    assert ("delete_object", {"Bucket": "test-bucket", "Key": "media/a.jpg"}) in client.calls
