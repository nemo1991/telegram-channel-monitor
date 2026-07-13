"""FolderObjectStore — 本地 FS,两级分片布局。

布局:`<root>/<key前2位>/<key后2位>/<key>` —— 例如
    key = "media/abcdef1234567890.jpg"
    落盘: <root>/me/di/media/abcdef1234567890.jpg

适用:大量小文件时,避免单目录 inode 压力;仍可用任何 FS 工具直接浏览。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import BinaryIO

from tgmonitor.core.objectstore.base import ObjectMeta, ObjectStore


_VALID_KEY = re.compile(r"^[A-Za-z0-9_./\-]+$")


class FolderObjectStore(ObjectStore):
    """两级分片:key 前 2 字符 + 后 2 字符做目录。"""

    backend_name = "folder"

    def __init__(self, root: Path, shard_size: int = 2) -> None:
        self._root = Path(root)
        self._shard = shard_size  # 0 = 不分片,等同平铺

    # ---- 生命周期 ----

    async def connect(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        return None

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

    def _ensure_inside_root(self, path: Path) -> None:
        try:
            path.relative_to(self._root.resolve())
        except ValueError as e:
            raise ValueError(f"key escapes root: {path}") from e

    # ---- 操作 ----

    async def put(self, key: str, data: bytes, meta: ObjectMeta | None = None) -> str:
        path = self._path(key)
        self._ensure_inside_root(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return key

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        self._ensure_inside_root(path)
        if not path.exists():
            raise KeyError(key)
        return path.read_bytes()

    async def exists(self, key: str) -> bool:
        path = self._path(key)
        self._ensure_inside_root(path)
        return path.exists()

    async def delete(self, key: str) -> None:
        path = self._path(key)
        self._ensure_inside_root(path)
        if path.exists():
            path.unlink()

    async def stat(self, key: str) -> ObjectMeta | None:
        path = self._path(key)
        self._ensure_inside_root(path)
        if not path.exists():
            return None
        return ObjectMeta(size=path.stat().st_size)

    async def open_read(self, key: str) -> BinaryIO:
        from io import BytesIO

        return BytesIO(await self.get(key))
