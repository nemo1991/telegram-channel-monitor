"""对象存储后端单测(Local + Folder + S3)。

2026-10-04 plan 阶段 1.3.B:`_FakeS3Client` / `_FakeStream` /
`_FakeClientContext` / `_FakePaginator` / `_FakePaginatorIter` /
`_FakeSession` 等替身 class 提到 `tests/fixtures/_s3_client.py`,
本文件 import 共用。本文件 `_*MissingBucketClient` 等子类继承
fixture 的 `FakeS3Client`,test 体特化的 method 仍留本文件。
"""

from __future__ import annotations

import hashlib
import threading

import aioboto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from tests.fixtures._s3_client import (
    FakeS3Client,
    FakeS3ClientContext,
    FakeS3Session,
)
from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.export.zip_exporter import ZipExporter
from tgmonitor.core.objectstore import s3_store as s3_store_mod
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


# =========================================================================
# 2026-11-25 P2 批次(#18 / #19 / #21 / #22 / #27)
# =========================================================================


# ---- #21:put 的 sha256 回填(Local 侧回归守卫;Folder 侧在 test_folder_store.py)----


async def test_local_put_backfills_sha256(tmp_path):
    """meta 给出且 sha256 为空 → put 自动算并回填(既有行为,上提基类后不变)。"""
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    meta = ObjectMeta(content_type="image/jpeg")
    await s.put("media/abc.jpg", b"hello", meta)
    assert meta.sha256 == hashlib.sha256(b"hello").hexdigest()
    # caller 已给 sha256 → 不覆盖
    meta2 = ObjectMeta(sha256="given")
    await s.put("media/def.jpg", b"hello", meta2)
    assert meta2.sha256 == "given"


# ---- #22:exists / delete / stat 同步 IO 走 to_thread ----


async def test_fs_exists_delete_stat_run_in_worker_thread(tmp_path, monkeypatch):
    """#22 回归:三个方法的同步 IO 必须在 worker 线程,不得阻塞事件循环。

    旧实现直接在 loop 线程调 Path.exists/stat/unlink — spy 记录到
    threading.get_ident() == 主线程 id 即失败。
    """
    from pathlib import Path

    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    await s.put("media/x.bin", b"data")  # put 本就走 to_thread,先于 spy 落盘
    main_tid = threading.get_ident()

    seen: list[tuple[str, int]] = []
    real_exists = Path.exists
    real_stat = Path.stat
    real_unlink = Path.unlink

    def spy_exists(self):
        seen.append(("exists", threading.get_ident()))
        return real_exists(self)

    def spy_stat(self, *a, **kw):
        seen.append(("stat", threading.get_ident()))
        return real_stat(self, *a, **kw)

    def spy_unlink(self, *a, **kw):
        seen.append(("unlink", threading.get_ident()))
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "exists", spy_exists)
    monkeypatch.setattr(Path, "stat", spy_stat)
    monkeypatch.setattr(Path, "unlink", spy_unlink)

    assert await s.exists("media/x.bin") is True
    meta = await s.stat("media/x.bin")
    assert meta is not None and meta.size == 4
    await s.delete("media/x.bin")
    assert not await s.exists("media/x.bin")

    ops = [op for op, _tid in seen]
    assert "exists" in ops and "stat" in ops and "unlink" in ops
    for op, tid in seen:
        assert tid != main_tid, f"{op} 在事件循环线程内同步执行(旧实现行为)— 必须走 to_thread"


# ---- #18:S3 exists / stat 区分「对象不存在」与其他错误 ----
# 旧实现 `except Exception` 吞掉一切 → 网络错误被解读为「不存在」→
# downloader 去重失效重复下载。新语义:404 类 → False / None;其他错误上抛。


@pytest.mark.parametrize("code", ["NoSuchKey", "NotFound", "404"])
async def test_s3_exists_missing_object_returns_false(monkeypatch, code):
    """三种「对象不存在」错误码 → False(不误伤为异常)。"""

    class _MissingObjectClient(FakeS3Client):
        async def head_object(self, **kw: object) -> dict:  # type: ignore[override]
            self._record("head_object", kw)
            raise _client_error(404, code)

    fake = FakeS3Session()
    fake._client_obj = _MissingObjectClient()
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="test-bucket", region="us-east-1")
    await store.connect()

    assert await store.exists("media/missing.jpg") is False
    assert await store.stat("media/missing.jpg") is None


@pytest.mark.parametrize(
    "exc",
    [
        _client_error(403, "AccessDenied"),
        EndpointConnectionError(endpoint_url="http://minio.local:9000"),
    ],
    ids=["access-denied", "network-error"],
)
async def test_s3_exists_non_404_error_raises(monkeypatch, exc):
    """权限 / 网络等真错误必须上抛 — 旧实现返 False(吞错),此测试失败。"""

    class _FailClient(FakeS3Client):
        async def head_object(self, **kw: object) -> dict:  # type: ignore[override]
            self._record("head_object", kw)
            raise exc

    fake = FakeS3Session()
    fake._client_obj = _FailClient()
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="test-bucket", region="us-east-1")
    await store.connect()

    with pytest.raises((ClientError, EndpointConnectionError)):
        await store.exists("media/x.jpg")
    with pytest.raises((ClientError, EndpointConnectionError)):
        await store.stat("media/x.jpg")


# ---- #19:put 大对象 multipart + client 复用 ----


class _UploadRecordingClient(FakeS3Client):
    """补 upload_fileobj 替身(#19/#27):读出 fileobj 内容供断言。"""

    async def upload_fileobj(self, fileobj, **kw: object) -> dict:
        body = fileobj.read()
        self._record("upload_fileobj", {**kw, "_body": body})
        return {}


async def _make_upload_store(monkeypatch) -> tuple[S3ObjectStore, _UploadRecordingClient]:
    fake = FakeS3Session()
    fake._client_obj = _UploadRecordingClient()
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="test-bucket", region="us-east-1")
    await store.connect()
    return store, fake._client_obj  # type: ignore[return-value]


async def test_s3_put_large_uses_upload_fileobj(monkeypatch):
    """#19:> 阈值走 upload_fileobj(multipart 分段,突破单次 PUT 5GB 上限)。"""
    monkeypatch.setattr(s3_store_mod, "_MULTIPART_THRESHOLD", 4)
    store, client = await _make_upload_store(monkeypatch)

    await store.put("media/big.bin", b"0123456789", ObjectMeta(content_type="video/mp4"))
    ups = [c for c in client.calls if c[0] == "upload_fileobj"]
    assert len(ups) == 1, f"大对象必须走 upload_fileobj: {client.calls}"
    kw = ups[0][1]
    assert kw["_body"] == b"0123456789"
    assert kw["Bucket"] == "test-bucket" and kw["Key"] == "media/big.bin"
    assert kw["ExtraArgs"] == {"ContentType": "video/mp4"}
    assert not [c for c in client.calls if c[0] == "put_object"]


async def test_s3_put_small_still_single_put(monkeypatch):
    """#19:≤ 阈值保持单次 put_object(零额外开销)。"""
    store, client = await _make_upload_store(monkeypatch)
    await store.put("media/small.bin", b"abc")
    assert [c for c in client.calls if c[0] == "put_object"]
    assert not [c for c in client.calls if c[0] == "upload_fileobj"]


async def test_s3_client_reused_across_operations(monkeypatch):
    """#19 回归:client 复用 — connect + 5 个操作全程只建 1 次 client。

    旧实现每个操作 `async with session.client(...)` 新建 client
    (TLS 握手 + 连接池重建 × 6);新实现懒初始化长生命周期 client。
    """
    fake = FakeS3Session()
    counter = {"n": 0}
    original_client = fake.client

    def counting_client(*a: object, **kw: object) -> FakeS3ClientContext:
        counter["n"] += 1
        return original_client(*a, **kw)

    monkeypatch.setattr(fake, "client", counting_client)
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="test-bucket", region="us-east-1")
    await store.connect()
    await store.put("a", b"x")
    await store.get("a")
    await store.exists("a")
    await store.stat("a")
    await store.delete("a")
    assert counter["n"] == 1, f"client 应复用,实际创建了 {counter['n']} 次(旧实现每操作新建)"


class _RecordingCtx(FakeS3ClientContext):
    """记录 __aexit__ 是否被调(#19 close / connect 失败释放路径)。"""

    exited: list[bool] = []

    async def __aexit__(self, *exc: object) -> bool:
        _RecordingCtx.exited.append(True)
        return await super().__aexit__(*exc)


async def test_s3_close_releases_client(monkeypatch):
    """#19:close() 走 ClientCreatorContext.__aexit__ 释放;close 后操作报未连接。"""

    _RecordingCtx.exited.clear()
    fake = FakeS3Session()
    fake.client = lambda *a, **kw: _RecordingCtx(fake._client_obj)
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="test-bucket", region="us-east-1")
    await store.connect()
    await store.put("a", b"x")
    await store.close()
    assert _RecordingCtx.exited, "close 必须退出 client context(释放连接池)"
    with pytest.raises(RuntimeError):
        await store.put("b", b"y")


async def test_s3_connect_failure_releases_client(monkeypatch):
    """connect 校验失败 → 释放半初始化 client,不留泄漏的连接。"""

    _RecordingCtx.exited.clear()

    class _ForbiddenClient(FakeS3Client):
        async def head_bucket(self, **kw: object) -> dict:
            self._record("head_bucket", kw)
            raise _client_error(403, "AccessDenied")

    fake = FakeS3Session()
    fake._client_obj = _ForbiddenClient()
    fake.client = lambda *a, **kw: _RecordingCtx(fake._client_obj)
    monkeypatch.setattr(aioboto3, "Session", lambda: fake)
    store = S3ObjectStore(bucket="test-bucket", region="us-east-1")
    with pytest.raises(ClientError):
        await store.connect()
    assert _RecordingCtx.exited, "connect 失败必须释放已建的 client"


# ---- #27:put_stream 接口 ----


async def test_local_put_stream_roundtrip(tmp_path):
    """#27:FS 后端逐块写文件,返回填好的 ObjectMeta(size + sha256)。"""
    s = LocalObjectStore(root=tmp_path)
    await s.connect()

    async def _chunks():
        for c in (b"ab", b"cdef", b"gh"):
            yield c

    meta = await s.put_stream("media/stream.bin", _chunks())
    assert await s.get("media/stream.bin") == b"abcdefgh"
    assert meta.size == 8
    assert meta.sha256 == hashlib.sha256(b"abcdefgh").hexdigest()


async def test_local_put_stream_error_cleans_part_file(tmp_path):
    """#27:中途异常 → 清理 .part 半成品,对象本体从不出现(原子性)。"""
    s = LocalObjectStore(root=tmp_path)
    await s.connect()

    async def _bad():
        yield b"good"
        raise RuntimeError("network boom")

    with pytest.raises(RuntimeError):
        await s.put_stream("media/x.bin", _bad())
    assert not (tmp_path / "media" / "x.bin").exists()
    assert not (tmp_path / "media" / "x.bin.part").exists()
    assert not await s.exists("media/x.bin")


async def test_local_put_stream_passes_content_type(tmp_path):
    """#27:meta.content_type 透传到返回的 ObjectMeta。"""
    s = LocalObjectStore(root=tmp_path)
    await s.connect()

    async def _chunks():
        yield b"x"

    meta = await s.put_stream("k", _chunks(), ObjectMeta(content_type="video/mp4"))
    assert meta.content_type == "video/mp4"


async def test_s3_put_stream_uploads_via_tempfile(monkeypatch):
    """#27:S3 put_stream — 临时文件 + upload_fileobj(multipart),内存 O(chunk)。"""

    async def _chunks():
        yield b"part1-"
        yield b"part2"

    store, client = await _make_upload_store(monkeypatch)
    meta = await store.put_stream(
        "media/stream.bin", _chunks(), ObjectMeta(content_type="video/mp4")
    )
    ups = [c for c in client.calls if c[0] == "upload_fileobj"]
    assert len(ups) == 1
    kw = ups[0][1]
    assert kw["_body"] == b"part1-part2"
    assert kw["Bucket"] == "test-bucket" and kw["Key"] == "media/stream.bin"
    assert kw["ExtraArgs"] == {"ContentType": "video/mp4"}
    assert meta.size == 11
    assert meta.sha256 == hashlib.sha256(b"part1-part2").hexdigest()
    assert meta.content_type == "video/mp4"


async def test_base_put_stream_default_raises():
    """#27:base 默认实现抛 NotImplementedError(第三方后端不强制实现)。"""
    from tgmonitor.core.objectstore.base import ObjectStore

    class _Stub(ObjectStore):
        backend_name = "stub"

        async def connect(self) -> None: ...
        async def close(self) -> None: ...
        async def put(self, key, data, meta=None) -> str:
            return key  # noqa: E704

        async def get(self, key) -> bytes:
            raise KeyError(key)  # noqa: E704

        async def exists(self, key) -> bool:
            return False  # noqa: E704

        async def delete(self, key) -> None: ...  # noqa: E704
        async def stat(self, key) -> ObjectMeta | None:
            return None  # noqa: E704

    stub = _Stub()

    async def _chunks():
        yield b"x"

    with pytest.raises(NotImplementedError):
        await stub.put_stream("k", _chunks())
