"""ObjectStore — 媒体二进制对象存储抽象。

- 接口全部 `async`
- `put/get/exists/delete` + 流式上下文 `open_read/open_write`(可选用)
- 后端有 Local 与 S3(aioboto3)两种,后端在 DB `media.object_backend` 字段标记
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, BinaryIO


@dataclass
class ObjectMeta:
    """对象元数据(从 put 时透传到 get 时返回)。"""

    content_type: str | None = None
    size: int | None = None
    sha256: str | None = None


class ObjectStore(ABC):
    """后端类型标识(写入 DB media.object_backend 字段,便于读时反查)。"""

    backend_name: str

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def put(self, key: str, data: bytes, meta: ObjectMeta | None = None) -> str:
        """存入对象,返回稳定 key(调用方可忽略返回值,用自己生成的 key 也行)。"""
        ...

    @abstractmethod
    async def get(self, key: str) -> bytes: ...

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def stat(self, key: str) -> ObjectMeta | None: ...

    # 可选流式 API(子类可 override 以利用大文件流式上传/下载)
    async def open_read(self, key: str) -> BinaryIO:  # pragma: no cover - 默认实现
        from io import BytesIO

        data = await self.get(key)
        return BytesIO(data)

    async def open_write(self, key: str, meta: ObjectMeta | None = None) -> BinaryIO:  # noqa: D401
        raise NotImplementedError

    async def iter_keys(self, prefix: str = "") -> AsyncIterator[str]:  # pragma: no cover
        if False:
            yield ""
