"""FolderObjectStore — 本地 FS,两级分片布局。

布局:`<root>/<目录前缀>/<文件名前2位>/<文件名第3-4位>/<key>`
(目录前缀按字面保留,分片针对**文件名**部分) —— 例如
    key = "media/abcdef1234567890.jpg"
    落盘: <root>/media/ab/cd/abcdef1234567890.jpg

适用:大量小文件时,避免单目录 inode 压力;仍可用任何 FS 工具直接浏览。

2026-11-25 P1-N3:`connect/close/_ensure_inside_root/get/exists/delete/
stat/open_read/stream_read` 与 Local 后端逐字重复,已上提到 `_fs_base.py`
共享基类。本模块只保留分片布局特有的 `__init__` / `_path` / `put` /
`iter_keys`。
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import AsyncIterator

from tgmonitor.core.objectstore._fs_base import LocalFsStoreBase
from tgmonitor.core.objectstore.base import ObjectMeta

_VALID_KEY = re.compile(r"^[A-Za-z0-9_./\-]+$")


class FolderObjectStore(LocalFsStoreBase):
    """两级分片:文件名前 2 字符 + 后 2 字符做目录(目录前缀按字面保留)。

    适用:大量小文件时避免单目录 inode 压力;仍可用任何 FS 工具直接浏览。
    """

    backend_name = "folder"

    def __init__(self, root: Path, shard_size: int = 2) -> None:
        """`root` = 根目录;`shard_size` = 分片前缀长度(0 = 不分片,等同平铺)。"""
        super().__init__(root)
        self._shard = shard_size  # 0 = 不分片,等同平铺

    # ---- 路径解析 ----

    def _path(self, key: str) -> Path:
        if not key or not _VALID_KEY.match(key) or ".." in key.split("/"):
            raise ValueError(f"invalid object key: {key!r}")
        if self._shard <= 0:
            return (self._root / key).resolve()
        # 分片针对**文件名**部分,目录前缀按字面保留
        if "/" in key:
            parent_str, _, name = key.rpartition("/")
            parent = Path(parent_str)
        else:
            parent = Path()
            name = key
        # 文件名太短就不分片
        if len(name) < self._shard * 2:
            return (self._root / parent / name).resolve()
        head = name[: self._shard]
        tail = name[self._shard : self._shard * 2]
        return (self._root / parent / head / tail / name).resolve()

    # ---- 操作 ----

    async def put(self, key: str, data: bytes, meta: ObjectMeta | None = None) -> str:
        """原子写:.part + rename;key 校验白名单字符 + 防越界。

        写盘是重 IO,放 `asyncio.to_thread` — 否则 FULL 策略下下载 100MB+ 视频
        同步写盘会阻塞 qasync 主事件循环,UI 卡顿。
        """
        path = self._path(key)
        self._ensure_inside_root(path)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 原子写:写到 .part 再 rename
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(path)

        await asyncio.to_thread(_write)
        return key

    async def iter_keys(self, prefix: str = "") -> AsyncIterator[str]:
        """枚举所有 key — 把分片布局展开成原始 key(2026-08-24 orphan reconcile)。

        落盘是两级分片 `media/ab/cd/<name>`,要返的是原始 key `media/<name>` —
        把路径里的分片目录(`media/<ab>/<cd>/`)重组成 `media/<name>`。
        走 to_thread 防 UI 卡顿。
        """
        root = self._root
        shard = self._shard

        def _walk() -> list[str]:
            keys: list[str] = []
            for dirpath, _dirnames, filenames in os.walk(root):
                for fn in filenames:
                    if ".tgmonitor_write_probe" in fn:
                        continue
                    full = Path(dirpath) / fn
                    rel_path = full.relative_to(root)
                    parts = list(rel_path.parts)
                    if not parts:
                        continue
                    # 重组:把分片布局展开成 `parent/name`。
                    # 例:`media / ab / cd / abc.jpg` → `media/abc.jpg`。
                    if (
                        shard > 0
                        and len(parts) >= 4
                        and parts[-1].startswith(
                            parts[-3] + parts[-2] + "",
                        )
                    ):
                        # 4-part:`<parent>/<head>/<tail>/<name>`,reverse 后是
                        # `<parent>/<name>`(head + tail 是 name 的前 2*shard 字符,
                        # 由 `_path` 写入时构造)。
                        parent = parts[:-3]
                        reconstructed = "/".join(parent + [parts[-1]])
                    elif shard > 0 and len(parts) == 3:
                        # 3-part:顶层 key(原 key 无 parent)分片后是
                        # `<head>/<tail>/<name>`,reverse 后只有 `<name>`。
                        # 旧实现的 3-part 分支
                        # (`parts[-1].startswith(parts[-2]) and len == shard`)
                        # 永远不命中(`name` 不会以 `tail` 开头,只可能以
                        # `head` 开头),直接掉到 else 分支返 `<head>/<tail>/<name>`,
                        # 与原 key 不一致;`reconcile_orphans` 把这个错的 key
                        # 当孤儿删,留下真正的 `<name>` dangling 在桶里。
                        # 旧版本 v1.5.0 引入时 `_path` 与 `make_key` 都要求
                        # key 带 `media/` 前缀,所以 4-part 路径恒真,3-part bug
                        # 不触发 — 仅在跨 backend 迁移 / 手动 put 顶层 key /
                        # 未来 `make_key` 改格式时浮出。
                        reconstructed = parts[-1]
                    else:
                        reconstructed = "/".join(parts)
                    if prefix and not reconstructed.startswith(prefix):
                        continue
                    keys.append(reconstructed)
            return keys

        items = await asyncio.to_thread(_walk)
        for k in items:
            yield k
