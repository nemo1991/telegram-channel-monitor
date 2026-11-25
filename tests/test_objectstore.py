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
from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.export.zip_exporter import ZipExporter
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


# ---- P1-10 回归(2026-11-25):S3 get / stream_read 缺失对象必须抛 KeyError ----
# 契约:`base.py::ObjectStore.get` — "不存在抛 KeyError";ZipExporter
# 只兜 KeyError。旧实现让 aioboto3 抛 ClientError(NoSuchKey),一条缺失
# media 让整包 zip 导出炸掉。


@pytest.mark.parametrize("code", ["NoSuchKey", "NotFound", "404"])
async def test_s3_get_missing_object_raises_keyerror(monkeypatch, code):
    """三种「对象不存在」错误码(真 AWS / MinIO 兼容实现)都必须转 KeyError。"""

    class _MissingObjectClient(FakeS3Client):
        async def get_object(self, **kw: object) -> dict:  # type: ignore[override]
            self._record("get_object", kw)
            raise _client_error(404, code)

    fake = FakeS3Session()
    fake._client_obj = _MissingObjectClient()
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="test-bucket", region="us-east-1")
    await store.connect()

    with pytest.raises(KeyError):
        await store.get("media/missing.jpg")


@pytest.mark.parametrize("code", ["NoSuchKey", "NotFound", "404"])
async def test_s3_stream_read_missing_object_raises_keyerror(monkeypatch, code):
    """stream_read 与 get 失败语义一致:缺失对象 → KeyError。"""

    class _MissingObjectClient(FakeS3Client):
        async def get_object(self, **kw: object) -> dict:  # type: ignore[override]
            self._record("get_object", kw)
            raise _client_error(404, code)

    fake = FakeS3Session()
    fake._client_obj = _MissingObjectClient()
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="test-bucket", region="us-east-1")
    await store.connect()

    with pytest.raises(KeyError):
        async for _ in store.stream_read("media/missing.jpg"):
            pass


async def test_s3_get_non_404_client_error_propagates(monkeypatch):
    """非「不存在」错误(如 AccessDenied)必须原样上抛 ClientError,不得误转 KeyError。"""

    class _DeniedClient(FakeS3Client):
        async def get_object(self, **kw: object) -> dict:  # type: ignore[override]
            self._record("get_object", kw)
            raise _client_error(403, "AccessDenied")

    fake = FakeS3Session()
    fake._client_obj = _DeniedClient()
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="test-bucket", region="us-east-1")
    await store.connect()

    with pytest.raises(ClientError):
        await store.get("media/denied.jpg")
    with pytest.raises(ClientError):
        async for _ in store.stream_read("media/denied.jpg"):
            pass


async def test_s3_zip_export_skips_missing_object(monkeypatch, tmp_path):
    """受害点回归:zip 导出遇到 S3 缺失对象只跳过该条,其余照常打包。

    旧实现 ClientError(NoSuchKey) 不被 `except KeyError` 捕获 → 整包炸;
    修后缺失条目跳过、存在的条目完整写入。
    """
    import zipfile

    from tests.fixtures._factories import make_photo
    from tgmonitor.core.dto import MediaDownloadStatus

    class _PartialMissingClient(FakeS3Client):
        """key 含 "missing" → NoSuchKey;其余正常返回 body。"""

        async def get_object(self, **kw: object) -> dict:  # type: ignore[override]
            self._record("get_object", kw)
            key = kw["Key"]
            if "missing" in key:
                raise _client_error(404, "NoSuchKey")
            return {"Body": _FakeStreamChunked(b"payload:" + key.encode())}

    fake = FakeS3Session()
    fake._client_obj = _PartialMissingClient()
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="test-bucket", region="us-east-1")
    await store.connect()

    msg_ok = make_photo(msg_id=1)
    msg_ok.media[0].download_status = MediaDownloadStatus.DONE
    msg_ok.media[0].object_key = "media/ok.jpg"
    msg_missing = make_photo(msg_id=2)
    msg_missing.media[0].download_status = MediaDownloadStatus.DONE
    msg_missing.media[0].object_key = "media/missing.jpg"

    out = tmp_path / "out.zip"
    exporter = ZipExporter()
    await exporter.render(
        out,
        {100: ChannelDTO(id=100, title="#100")},
        [msg_ok, msg_missing],
        object_store=store,
    )

    with zipfile.ZipFile(out) as zf:
        names = [n for n in zf.namelist() if n != "_manifest.json"]
    # 缺失条目跳过:entry 存在但为空(stream_read 在首块前抛 KeyError,
    # 未写入任何字节),而不是让整个 render 抛异常炸包 — 与 Local 后端
    # 缺失文件的行为一致(zip_exporter 只兜 KeyError,entry 已被 zf.open 创建)。
    assert names == ["1_0_pic.jpg", "2_0_pic.jpg"]
    with zipfile.ZipFile(out) as zf:
        assert zf.read("1_0_pic.jpg") == b"payload:media/ok.jpg"
        assert zf.read("2_0_pic.jpg") == b""


# ---- P1-11 回归(2026-11-25):Local/Folder stream_read 必须真流式 ----


async def test_local_stream_read_reads_incrementally(tmp_path, monkeypatch):
    """逐块读盘回归:第一个 chunk yield 前,文件不得被全量读入内存。

    旧实现 `list(_chunks())` 把整个文件读进 list 才开始 yield — 消费
    第一块时累计 read 字节数 = 整个文件大小(O(file_size) 假流式);
    真流式实现下第一块 yield 时累计 read ≤ 2 块(O(chunk_size))。
    """
    from pathlib import Path

    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    chunk_size = 4096
    data = b"x" * (10 * 1024)
    await s.put("media/big.bin", data)

    # spy 文件对象:记录每次 read 的字节数(在 put 之后 monkeypatch,不影响写入)
    real_open = Path.open
    reads: list[int] = []
    closed: list[bool] = []

    class _SpyFile:
        def __init__(self, f) -> None:
            self._f = f

        def read(self, size: int = -1) -> bytes:
            chunk = self._f.read(size)
            reads.append(len(chunk))
            return chunk

        def close(self) -> None:
            closed.append(True)
            self._f.close()

    def spy_open(self, mode: str = "r", *args, **kwargs):
        f = real_open(self, mode, *args, **kwargs)
        if mode == "rb":
            return _SpyFile(f)
        return f

    monkeypatch.setattr(Path, "open", spy_open)

    agen = s.stream_read("media/big.bin", chunk_size=chunk_size)
    first = await agen.__anext__()
    assert first == data[:chunk_size]
    # 关键断言:第一块 yield 时只读到了第一块(允许一次 4096 + 一次部分 read)
    assert sum(reads) <= 2 * chunk_size, (
        f"假流式:第一块 yield 前已累计读取 {sum(reads)} 字节(文件 {len(data)} 字节)"
    )
    # 继续消费完,chunk 边界语义不变
    rest = [c async for c in agen]
    assert b"".join([first, *rest]) == data
    # 句柄被关闭(耗尽路径)
    assert closed, "stream_read 耗尽后文件句柄必须关闭"


async def test_local_stream_read_closes_handle_on_abandon(tmp_path, monkeypatch):
    """取消 / 提前退出路径:async gen 被 aclose 时文件句柄必须关闭(try/finally)。"""
    from pathlib import Path

    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    await s.put("media/big.bin", b"x" * (10 * 1024))

    real_open = Path.open
    closed: list[bool] = []

    class _SpyFile:
        def __init__(self, f) -> None:
            self._f = f

        def read(self, size: int = -1) -> bytes:
            return self._f.read(size)

        def close(self) -> None:
            closed.append(True)
            self._f.close()

    def spy_open(self, mode: str = "r", *args, **kwargs):
        f = real_open(self, mode, *args, **kwargs)
        if mode == "rb":
            return _SpyFile(f)
        return f

    monkeypatch.setattr(Path, "open", spy_open)

    agen = s.stream_read("media/big.bin", chunk_size=1024)
    await agen.__anext__()  # 消费一块后放弃
    await agen.aclose()
    assert closed, "aclose(GeneratorExit) 后文件句柄必须关闭(try/finally)"
