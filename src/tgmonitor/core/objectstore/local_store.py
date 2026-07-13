"""Local filesystem adapter — 开发 / CI / 离线用。

- key 形如 `media/<sha256>.jpg`,文件落在 `root / key`
- 防越界:禁止 `..` 与绝对路径
- 内容寻址:典型用法是 `put(sha256_of_bytes, bytes)`,天然去重
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

from tgmonitor.core.objectstore.base import ObjectMeta, ObjectStore


def _safe_key(key: str) -> Path:
    """校验 key 合法性,返回绝对路径(被 `root` 约束)。"""
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise ValueError(f"invalid object key: {key!r}")
    return Path(key)


class LocalObjectStore(ObjectStore):
    backend_name = "local"

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    # ---- 生命周期 ----

    async def connect(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
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
        path = self._path(key)
        self._ensure_inside_root(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写:写到 .part 再 rename
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        # 若 caller 没传 sha256,自动算一个
        if meta is not None and meta.sha256 is None:
            meta.sha256 = hashlib.sha256(data).hexdigest()
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
        # 显式继承默认实现即可(BytesIO)
        return await super().open_read(key)
