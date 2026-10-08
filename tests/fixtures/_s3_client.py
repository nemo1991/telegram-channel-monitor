"""共享 aioboto3 替身 — 2026-10-04 plan 阶段 1.3.B 从
`tests/test_objectstore.py` + `tests/test_media_manager.py` 各自内嵌
class 合并提到此处。

设计:提供 5 个 class,覆盖两个原文件需要的所有面:
- `FakeS3Stream` — StreamingBody 替身(异步 ctx + `read()` 返回整段 bytes)
- `FakeS3Paginator` — boto3 paginator 替身(单页 / 多页)
- `FakeS3ClientContext` — aioboto3 `ClientCreatorContext`(async with)
- `FakeS3Client` — boto3 s3 client 替身,完整实现:
  `head_bucket` / `create_bucket` / `put_object` / `get_object` /
  `head_object` / `delete_object` / `get_paginator` + `set_list_keys`
- `FakeS3Session` — aioboto3 Session 替身(直接通过 `client()` 返回 context)

调用约定:
- `set_list_keys(keys)` 注入 paginator 返的 keys
- `set_data(data)` / `set_raise(exc)` 控制 get_object 行为(给
  open_media 失败 / 边界 case 用)
- 所有 `**kw` 都原样记录到 `client.calls: list[(op, kw)]`,test 断言用

命名:`Fake*`(无前导 _)— 同 module 内 fixture 公开 API,允许 `from
tests.fixtures._s3_client import FakeS3Client, FakeS3Session` 复用。
其余 11 个 fixture 子模块也走这个约定(`_factories.make_message` 是
函数不是 class,公开仍用 module-level 函数)。
"""

from __future__ import annotations


class FakeS3Stream:
    """最小 aioboto3 StreamingBody 替身 — async ctx + `read()` 返整段。

    `read()` 不分块,直接 `_data`(测试用例短数据足够);若需要分块
    流控,继承 + override `read` 即可。
    """

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self) -> FakeS3Stream:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def read(self) -> bytes:
        return self._data


class FakeS3ClientContext:
    """aioboto3 ClientCreatorContext 替身 — `async with` 进入后返 inner client。

    关键行为:`Session.client()` 返回的是**异步上下文管理器本身**,
    必须 `async with` 进入后才能拿到真正的 client —— 修复前 s3_store
    直接把 context 对象当 client 用,`put_object` 等调用全部
    AttributeError。本替身忠实模拟这条契约。
    """

    def __init__(self, client: FakeS3Client) -> None:
        self._client = client

    async def __aenter__(self) -> FakeS3Client:
        return self._client

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakeS3Paginator:
    """mock boto3 paginator:把 keys 列表切成 N 页(每页 page_size)返。

    真 boto3 的分页行为:每页一个 batch;batch 抽完后 StopAsyncIteration。
    page_size 默认 1000(等同 boto3 默认 PaginationConfig.PageSize 上限)。
    """

    def __init__(self, keys: list, page_size: int = 1000) -> None:
        self._keys = list(keys)
        self._page_size = page_size

    def paginate(self, **kw: object) -> FakeS3PaginatorIter:
        return FakeS3PaginatorIter(self._keys, self._page_size)


class FakeS3PaginatorIter:
    """async iterator,每页一个 batch;抽完 StopAsyncIteration。"""

    def __init__(self, keys: list, page_size: int) -> None:
        self._keys = list(keys)
        self._page_size = page_size

    def __aiter__(self) -> FakeS3PaginatorIter:
        return self

    async def __anext__(self) -> dict:
        if not self._keys:
            raise StopAsyncIteration
        batch = self._keys[: self._page_size]
        del self._keys[: self._page_size]
        return {"Contents": [{"Key": k} for k in batch]}


class FakeS3Client:
    """boto3 s3 client 替身 — 覆盖 `core/objectstore/s3_store.py` 用的所有方法。

    调用记录:`self.calls: list[(op, kw)]` —— test 断言 `head_bucket` /
    `put_object` 等是否调到、调用次数、参数。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        # iter_keys 测试:模拟 list_objects_v2 的 keys(可被子类覆盖)
        self._list_keys: list[str] = []
        # get_object 结果 / 失败控制(open_media_with_result 失败路径用)
        self._data: bytes = b"data"
        self._raise: Exception | None = None

    def set_list_keys(self, keys: list[str]) -> None:
        """注入 paginator 返回的 key 列表。"""
        self._list_keys = list(keys)

    def set_data(self, data: bytes) -> None:
        """注入 `get_object` 返回的字节(默认 `b"data"`)。"""
        self._data = data

    def set_raise(self, exc: Exception | None) -> None:
        """注入 `get_object` 抛的异常(测试失败路径用,默认 None)。"""
        self._raise = exc

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
        if self._raise is not None:
            raise self._raise
        return {"Body": FakeS3Stream(self._data)}

    async def head_object(self, **kw: object) -> dict:
        self._record("head_object", kw)
        return {"ContentType": "image/jpeg", "ContentLength": 4}

    async def delete_object(self, **kw: object) -> dict:
        self._record("delete_object", kw)
        return {}

    def get_paginator(self, name: str) -> FakeS3Paginator:
        """fake paginator — 单页返所有 keys,真 boto3 是按页切分。"""
        self._record("get_paginator", {"name": name})
        return FakeS3Paginator(self._list_keys)


class FakeS3Session:
    """aioboto3.Session 替身 — `Session().client('s3')` 返一个 ClientCreatorContext。"""

    def __init__(self, client: FakeS3Client | None = None) -> None:
        self._client_obj = client if client is not None else FakeS3Client()

    def client(self, *args: object, **kw: object) -> FakeS3ClientContext:
        return FakeS3ClientContext(self._client_obj)
