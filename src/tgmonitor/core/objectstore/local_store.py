"""Local filesystem adapter — 开发 / CI / 离线用。

- key 形如 `media/<sha256>.jpg`,文件落在 `root / key`
- 防越界:禁止 `..` 与绝对路径
- 内容寻址:典型用法是 `put(sha256_of_bytes, bytes)`,天然去重
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Iterator

from tgmonitor.core.objectstore.base import ObjectMeta, ObjectStore, probe_writable


def _safe_key(key: str) -> Path:
    """校验 key 合法性,返回绝对路径(被 `root` 约束)。"""
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise ValueError(f"invalid object key: {key!r}")
    return Path(key)


class LocalObjectStore(ObjectStore):
    """平铺 FS 后端:`<root>/<key>` 一一对应。

    适用场景:开发 / CI / 小规模单机部署;零外部依赖。
    """

    backend_name = "local"

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
        # 本地 FS 无连接
        return None

    # ---- 路径解析 ----

    def _path(self, key: str) -> Path:
        rel = _safe_key(key)
        return (self._root / rel).resolve()

    def _ensure_inside_root(self, path: Path) -> None:
        try:
            path.relative_to(self._root.resolve())
        except ValueError as e:
            raise ValueError(f"key escapes root: {path}") from e

    # ---- 操作 ----

    async def put(self, key: str, data: bytes, meta: ObjectMeta | None = None) -> str:
        """原子写:.part + rename;若 caller 没传 sha256,自动补一个到 meta。

        写盘 + sha256 是重 IO/CPU,放 `asyncio.to_thread` — 否则 FULL 策略下
        下载 100MB+ 视频同步写盘会阻塞 qasync 主事件循环,UI 卡顿。
        """
        path = self._path(key)
        self._ensure_inside_root(path)

        def _write() -> str | None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 原子写:写到 .part 再 rename
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(path)
            # 若 caller 没传 sha256,自动算一个(大文件哈希也 off-loop)
            if meta is not None and meta.sha256 is None:
                return hashlib.sha256(data).hexdigest()
            return None

        digest = await asyncio.to_thread(_write)
        if meta is not None and digest is not None:
            meta.sha256 = digest
        return key

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
        """默认 `BytesIO(await get())`;Local 不 override streaming。"""
        # 显式继承默认实现即可(BytesIO)
        return await super().open_read(key)

    async def stream_read(
        self,
        key: str,
        chunk_size: int = 65536,
    ) -> AsyncIterator[bytes]:
        """Local 真流式 — 2026-09-02 v1.5.2 PR #B6。

        sync `open()` + `read(chunk_size)` 包成 sync generator,整个迭代
        走 `asyncio.to_thread` 让出 qasync 主 loop。调用方代码可正常
        `async for chunk in store.stream_read(key)` — 内存峰值 O(chunk_size)。

        与 `get()` 失败语义对齐:不存在 → `KeyError`。

        注:此实现把 sync gen 一次性 `list()` 再 yield,完整迭代仍会缓冲
        所有 chunk(因为 sync IO 不支持真 async suspend);对**单个**
        zip export 场景够用(单文件 64KB×N 的 chunk 数 → NKB 缓冲)。
        真零拷贝需要 `aiofiles` 或 `loop.add_reader`,留 v1.5.3。
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

    async def iter_keys(self, prefix: str = "") -> AsyncIterator[str]:
        """枚举所有 key — `os.walk(root)` 走 to_thread,产相对路径 key。

        2026-08-24 orphan reconcile 用:扫 ObjectStore 里所有 keys,跟 storage
        媒体索引对比,差集 = 孤儿。FS 遍历是阻塞 IO,放 to_thread 防 UI 卡顿。
        """
        root = self._root

        def _walk() -> Iterator[str]:
            for dirpath, _dirnames, filenames in os.walk(root):
                for fn in filenames:
                    full = Path(dirpath) / fn
                    rel = os.path.relpath(full, root)
                    # object key 一律用 POSIX 分隔符(`/`)— Windows 上 `os.walk`
                    # 与 `os.path.relpath` 返 `\` 分隔,与 put/get/delete 的 key
                    # 入参(用户代码写 `media/x.jpg`)不一致会直接坏掉 prefix 过滤。
                    # 2026-08-29 v1.5.0 CI 修复:Windows path 失败(`iter_keys`
                    # 返 `media\abc.jpg` 但 prefix=`media/` 不匹配)。
                    rel = rel.replace(os.sep, "/")
                    # 跳过探针文件(connect 时 probe_writable 创建/删除,可能短暂存在)
                    if ".tgmonitor_write_probe" in fn:
                        continue
                    if prefix and not rel.startswith(prefix):
                        continue
                    yield rel

        async def _to_thread_iter(sync_it):
            """把同步 generator 转 async,中间用 to_thread 让出 loop。"""
            # 一次性收集到 list 再 yield(简单且 O(N))。后续若 N 大可改 chunked。
            items = await asyncio.to_thread(list, _walk())
            for k in items:
                yield k

        async for k in _to_thread_iter(None):
            yield k
