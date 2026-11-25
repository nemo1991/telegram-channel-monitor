"""Local filesystem adapter — 开发 / CI / 离线用。

- key 形如 `media/<sha256>.jpg`,文件落在 `root / key`
- 防越界:禁止 `..` 与绝对路径
- 内容寻址:典型用法是 `put(sha256_of_bytes, bytes)`,天然去重

2026-11-25 P1-N3:`connect/close/_ensure_inside_root/get/exists/delete/
stat/open_read/stream_read` 与 Folder 后端逐字重复,已上提到 `_fs_base.py`
共享基类。本模块只保留平铺布局特有的 `_path` / `put`(sha256 自动补) /
`iter_keys`。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import AsyncIterator, Iterator

from tgmonitor.core.objectstore._fs_base import LocalFsStoreBase
from tgmonitor.core.objectstore.base import ObjectMeta


def _safe_key(key: str) -> Path:
    """校验 key 合法性,返回绝对路径(被 `root` 约束)。"""
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise ValueError(f"invalid object key: {key!r}")
    return Path(key)


class LocalObjectStore(LocalFsStoreBase):
    """平铺 FS 后端:`<root>/<key>` 一一对应。

    适用场景:开发 / CI / 小规模单机部署;零外部依赖。
    """

    backend_name = "local"

    # ---- 路径解析 ----

    def _path(self, key: str) -> Path:
        rel = _safe_key(key)
        return (self._root / rel).resolve()

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
