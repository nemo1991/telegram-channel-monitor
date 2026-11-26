"""本地文件系统 ObjectStore 共享基类 — 2026-11-25 P1-N3。

`LocalObjectStore`(平铺)与 `FolderObjectStore`(两级分片)在以下方法上
共享实现:connect / close / _ensure_inside_root / get / exists / delete /
stat / open_read / stream_read / put / put_stream。这些方法只依赖
`_path()` 的布局结果,与分片策略无关 → 收口到本基类,子类只实现
差异化的部分:

- `_path(key)` — 布局(平铺 vs 分片),抽象
- `iter_keys(prefix)` — 把落盘布局反向解析回原始 key,各后端不同
- `_post_put(key, path)` — 落盘后的后端钩子(默认 no-op;Folder 写旁车)

2026-11-25 #21:`put` 的 sha256 回填上提到本基类 — 此前 Local 回填、
Folder 不回填,Folder 后端 thumbnails.sha256 恒 NULL;现在两子类行为
一致(meta 给出且 sha256 为空 → 自动算并回填)。

2026-11-25 #22:`exists/delete/stat` 的同步 IO 包 `asyncio.to_thread`,
与 get/put 一致 — 否则事件循环内同步 stat 会阻塞 qasync 主 loop。

2026-11-25 #27:`put_stream` 逐块写文件(边写边算 sha256),内存
O(chunk),写完 rename 收口原子性,行为与 `put` 一致。
"""

from __future__ import annotations

import asyncio
import hashlib
from abc import abstractmethod
from pathlib import Path
from typing import AsyncIterator, BinaryIO

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

    # ---- 写入(Local / Folder 共用,#21 / #27)----

    @staticmethod
    def _write_bytes_atomic(path: Path, data: bytes, hash_needed: bool) -> str | None:
        """原子写:.part + rename;`hash_needed` 时顺手算 sha256。

        写盘 + sha256 是重 IO/CPU,由调用方放 `asyncio.to_thread` —
        否则 FULL 策略下下载 100MB+ 视频同步写盘会阻塞 qasync 主事件
        循环,UI 卡顿。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        if hash_needed:
            return hashlib.sha256(data).hexdigest()
        return None

    def _post_put(self, key: str, path: Path) -> None:
        """落盘后的后端钩子(同步方法,由 put/put_stream 在 worker 线程内调用)。

        默认 no-op;FolderObjectStore override 写 `.origkey` 旁车(#20)。
        """
        return None

    async def put(self, key: str, data: bytes, meta: ObjectMeta | None = None) -> str:
        """原子写:.part + rename;若 caller 没传 sha256,自动补一个到 meta。

        2026-11-25 #21:Local / Folder 行为统一 — 此前 Folder 不回填,
        `thumbnails.sha256` 在 Folder 后端恒 NULL。
        """
        path = self._path(key)
        self._ensure_inside_root(path)
        hash_needed = meta is not None and meta.sha256 is None

        def _write() -> str | None:
            digest = self._write_bytes_atomic(path, data, hash_needed)
            self._post_put(key, path)
            return digest

        digest = await asyncio.to_thread(_write)
        if digest is not None and meta is not None:
            meta.sha256 = digest
        return key

    async def put_stream(
        self,
        key: str,
        chunk_iter: AsyncIterator[bytes],
        meta: ObjectMeta | None = None,
    ) -> ObjectMeta:
        """逐块写文件(2026-11-25 #27)— 内存 O(chunk)。

        边写边喂 sha256;中途异常(网络断 / 生成器抛错)清理 .part 半成品
        并上抛,不留脏数据。写完 rename 收口,原子性与 `put` 一致。
        """
        path = self._path(key)
        self._ensure_inside_root(path)
        tmp = path.with_suffix(path.suffix + ".part")

        def _open_tmp():
            path.parent.mkdir(parents=True, exist_ok=True)
            return tmp.open("wb")

        f = await asyncio.to_thread(_open_tmp)
        size = 0
        digest = hashlib.sha256()
        try:
            try:
                async for chunk in chunk_iter:
                    size += len(chunk)
                    digest.update(chunk)
                    await asyncio.to_thread(f.write, chunk)
            finally:
                await asyncio.to_thread(f.close)
            await asyncio.to_thread(tmp.replace, path)
        except BaseException:
            # 中途失败:清掉 .part 半成品(对象本体从未出现,原子性保持)
            await asyncio.to_thread(lambda: tmp.unlink(missing_ok=True))
            raise
        await asyncio.to_thread(self._post_put, key, path)
        return ObjectMeta(
            content_type=meta.content_type if meta is not None else None,
            size=size,
            sha256=digest.hexdigest(),
        )

    # ---- 操作 ----

    async def get(self, key: str) -> bytes:
        """读全量 bytes;不存在抛 `KeyError`。读盘走 to_thread 不阻塞 loop。"""
        path = self._path(key)
        self._ensure_inside_root(path)
        if not path.exists():
            raise KeyError(key)
        return await asyncio.to_thread(path.read_bytes)

    async def exists(self, key: str) -> bool:
        """是否存在(比 try get + 捕获 KeyError 轻)。

        2026-11-25 #22:Path.exists 是同步 IO,包 to_thread 不阻塞 loop
        (与 get/put 的处理一致)。
        """
        path = self._path(key)
        self._ensure_inside_root(path)
        return await asyncio.to_thread(path.exists)

    async def delete(self, key: str) -> None:
        """删除;不存在不抛(idempotent)。同步 unlink 走 to_thread(#22)。"""
        path = self._path(key)
        self._ensure_inside_root(path)

        def _do() -> None:
            if path.exists():
                path.unlink()

        await asyncio.to_thread(_do)

    async def stat(self, key: str) -> ObjectMeta | None:
        """拿 size;不存在返 None(不抛)。同步 stat 走 to_thread(#22)。"""
        path = self._path(key)
        self._ensure_inside_root(path)

        def _do() -> ObjectMeta | None:
            if not path.exists():
                return None
            return ObjectMeta(size=path.stat().st_size)

        return await asyncio.to_thread(_do)

    async def open_read(self, key: str) -> BinaryIO:
        """默认 `BytesIO(await get())` — 本地 FS 不 override streaming。"""
        return await super().open_read(key)

    async def stream_read(
        self,
        key: str,
        chunk_size: int = 65536,
    ) -> AsyncIterator[bytes]:
        """本地 FS 真流式 — 2026-09-02 v1.5.2 PR #B6。

        `open()` + 逐块 `read(chunk_size)` + `asyncio.to_thread` 让出
        qasync 主 loop。与 `get()` 失败语义对齐:不存在 → `KeyError`。

        2026-11-25 P1-11:改真流式。旧实现把 sync generator 一次性
        `list()` 收满再逐块 yield — 内存峰值 O(file_size),与 zip_exporter
        docstring "内存峰值 O(chunk_size)" 矛盾。现在每次 `__anext__` 只
        读一块,内存峰值 O(chunk_size);chunk 边界语义不变(末块可短于
        chunk_size)。

        文件句柄:`try/finally` 保证 async gen 正常耗尽 / break / aclose /
        异常路径都关闭(to_thread(close) 同步关,不阻塞 loop)。
        """
        path = self._path(key)
        self._ensure_inside_root(path)
        if not path.exists():
            raise KeyError(key)

        f = await asyncio.to_thread(path.open, "rb")
        try:
            while True:
                chunk = await asyncio.to_thread(f.read, chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(f.close)
