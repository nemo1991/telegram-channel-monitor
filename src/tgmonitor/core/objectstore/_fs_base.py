"""本地文件系统 ObjectStore 共享基类 — 2026-11-25 P1-N3。

`LocalObjectStore`(平铺)与 `FolderObjectStore`(两级分片)在以下方法上
逐字重复:connect / close / _ensure_inside_root / get / exists / delete /
stat / open_read / stream_read。这些方法只依赖 `_path()` 的布局结果,
与分片策略无关 → 收口到本基类,子类只实现差异化的部分:

- `_path(key)` — 布局(平铺 vs 分片),抽象
- `put(key, data, meta)` — Local 自动补 sha256、Folder 不补,语义不同
- `iter_keys(prefix)` — 把落盘布局反向解析回原始 key,各后端不同

行为不变:所有方法体从原两个类逐字搬移(含 to_thread 防 UI 卡顿)。
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Iterator

from tgmonitor.core.objectstore.base import ObjectMeta, ObjectStore, probe_writable


class LocalFsStoreBase(ObjectStore):
    """本地 FS 后端骨架 — 子类提供 `_path()` 定义 key → 绝对路径布局。"""

    def __init__(self, root: Path) -> None:
        """`root` = 对象存储根目录(必须已存在或 connect 时创建)。"""
        self._root = Path(root)

    # ---- 生命周期 ----

    async def connect(self) -> None:
        """确保 root 目录存在且可写(配置变更保存前做真实写探测)。

        mkdir(exist_ok=True) 对「目录已存在但不可写」不报错,写入时才失败;
        加一步探针写/删,权限问题在保存设置时就暴露(2026-08-18)。
        """
        self._root.mkdir(parents=True, exist_ok=True)
        probe_writable(self._root)

    async def close(self) -> None:
        """本地 FS 无连接 — no-op。"""
        return None

    # ---- 路径解析 ----

    @abstractmethod
    def _path(self, key: str) -> Path:
        """key → 绝对路径;子类定义布局与 key 合法性校验。"""
        ...

    def _ensure_inside_root(self, path: Path) -> None:
        try:
            path.relative_to(self._root.resolve())
        except ValueError as e:
            raise ValueError(f"key escapes root: {path}") from e

    def local_path(self, key: str) -> Path | None:
        """P1-C3 能力:本地 FS 后端都有真实落盘路径 → 委托 `_path()`。"""
        return self._path(key)

    # ---- 操作 ----

    async def get(self, key: str) -> bytes:
        """读全量 bytes;不存在抛 `KeyError`。读盘走 to_thread 不阻塞 loop。"""
        path = self._path(key)
        self._ensure_inside_root(path)
        if not path.exists():
            raise KeyError(key)
        return await asyncio.to_thread(path.read_bytes)

    async def exists(self, key: str) -> bool:
        """是否存在(比 try get + 捕获 KeyError 轻)。"""
        path = self._path(key)
        self._ensure_inside_root(path)
        return path.exists()

    async def delete(self, key: str) -> None:
        """删除;不存在不抛(idempotent)。"""
        path = self._path(key)
        self._ensure_inside_root(path)
        if path.exists():
            path.unlink()

    async def stat(self, key: str) -> ObjectMeta | None:
        """拿 size;不存在返 None(不抛)。"""
        path = self._path(key)
        self._ensure_inside_root(path)
        if not path.exists():
            return None
        return ObjectMeta(size=path.stat().st_size)

    async def open_read(self, key: str) -> BinaryIO:
        """默认 `BytesIO(await get())` — 本地 FS 不 override streaming。"""
        return await super().open_read(key)

    async def stream_read(
        self,
        key: str,
        chunk_size: int = 65536,
    ) -> AsyncIterator[bytes]:
        """本地 FS 真流式 — 2026-09-02 v1.5.2 PR #B6。

        sync `open()` + `read(chunk_size)` 包成 sync generator,整个迭代
        走 `asyncio.to_thread` 让出 qasync 主 loop。与 `get()` 失败语义
        对齐:不存在 → `KeyError`。

        注:此实现把 sync gen 一次性 `list()` 再 yield,完整迭代仍会缓冲
        所有 chunk(单个 zip export 场景够用);真零拷贝需 aiofiles,留后议。
        """
        path = self._path(key)
        self._ensure_inside_root(path)
        if not path.exists():
            raise KeyError(key)

        def _chunks() -> Iterator[bytes]:
            with path.open("rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        return
                    yield chunk

        items = await asyncio.to_thread(list, _chunks())
        for c in items:
            yield c
