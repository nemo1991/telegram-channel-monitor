"""ObjectStore — 媒体二进制对象存储抽象。

- 接口全部 `async`
- `put/get/exists/delete` + 流式上下文 `open_read/open_write`(可选用)
- 后端有 Local 与 S3(aioboto3)两种,后端在 DB `media.object_backend` 字段标记
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, BinaryIO


@dataclass
class ObjectMeta:
    """对象元数据(从 put 时透传到 get 时返回)。"""

    content_type: str | None = None
    size: int | None = None
    sha256: str | None = None


def probe_writable(root: Path) -> None:
    """真实写探测:目录已存在但不可写时 `mkdir(exist_ok=True)` 不报错,
    写入时才失败。connect() 在保存设置 / 启动时调它,提前暴露权限问题
    (2026-08-18,与 S3 connect 校验对齐)。失败抛 `PermissionError`。
    """
    probe = root / ".tgmonitor_write_probe"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise PermissionError(f"对象存储目录不可写: {root}") from exc


class ObjectStore(ABC):
    """后端类型标识(写入 DB media.object_backend 字段,便于读时反查)。"""

    backend_name: str

    @abstractmethod
    async def connect(self) -> None:
        """初始化后端连接 / 建桶 / 建目录等(AppService bootstrap 时调一次)。"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """释放连接 / 句柄(close lifecycle 末尾调一次)。"""
        ...

    @abstractmethod
    async def put(self, key: str, data: bytes, meta: ObjectMeta | None = None) -> str:
        """存入对象,返回稳定 key(调用方可忽略返回值,用自己生成的 key 也行)。

        原子写由后端保证:Local 走 .part + rename,S3 走 multipart。
        """
        ...

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """读全量 bytes;不存在抛 `KeyError`。"""
        ...

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """是否存在(比 `try get` + 捕获 KeyError 轻)。"""
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """删除;不存在不抛(idempotent)。"""
        ...

    @abstractmethod
    async def stat(self, key: str) -> ObjectMeta | None:
        """拿元数据;不存在返 None(不抛)。"""
        ...

    # 可选流式 API(子类可 override 以利用大文件流式上传/下载)
    async def open_read(self, key: str) -> BinaryIO:  # pragma: no cover - 默认实现
        """打开读流 — 默认 `BytesIO(await self.get(key))`,子类可 override 做 streaming。"""
        from io import BytesIO

        data = await self.get(key)
        return BytesIO(data)

    async def open_write(self, key: str, meta: ObjectMeta | None = None) -> BinaryIO:  # noqa: D401
        """打开写流;默认 `NotImplementedError`(Local 不支持 seek-write,S3 / FS 才用)。"""
        raise NotImplementedError

    async def stream_read(  # pragma: no cover - 默认实现
        self,
        key: str,
        chunk_size: int = 65536,
    ) -> AsyncIterator[bytes]:
        """分块流式读 — 默认 `get()` 切片 yield,子类 override 走真 IO 流式。

        2026-09-02 v1.5.2 PR #B6:为 `ZipExporter` 大文件导出铺垫 — 走
        `stream_read` 后,内存峰值从 O(file_size) 降到 O(chunk_size)。
        失败语义与 `get()` 一致(不存在抛 `KeyError`);中途异常应使
        `async for` 正常终止,调用方按需捕获。

        默认实现仍走 `get()` 全量读(向后兼容,行为与 v1.5.1 完全一致);
        Local / Folder / S3 三个生产后端都已 override,实际运行时是流式。
        """
        if False:
            yield b""  # 让 mypy 识别 AsyncIterator[bytes] generator(同 iter_keys 模式)
        data = await self.get(key)
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    async def iter_keys(self, prefix: str = "") -> AsyncIterator[str]:  # pragma: no cover
        """按前缀枚举所有 key;默认 no-op(后端支持再 override)。"""
        if False:
            yield ""
