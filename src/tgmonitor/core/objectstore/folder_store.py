"""FolderObjectStore — 本地 FS,两级分片布局。

布局:`<root>/<目录前缀>/<文件名前2位>/<文件名第3-4位>/<key>`
(目录前缀按字面保留,分片针对**文件名**部分) —— 例如
    key = "media/abcdef1234567890.jpg"
    落盘: <root>/media/ab/cd/abcdef1234567890.jpg

适用:大量小文件时,避免单目录 inode 压力;仍可用任何 FS 工具直接浏览。
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import AsyncIterator, BinaryIO

from tgmonitor.core.objectstore.base import ObjectMeta, ObjectStore, probe_writable

_VALID_KEY = re.compile(r"^[A-Za-z0-9_./\-]+$")


class FolderObjectStore(ObjectStore):
    """两级分片:文件名前 2 字符 + 后 2 字符做目录(目录前缀按字面保留)。

    适用:大量小文件时避免单目录 inode 压力;仍可用任何 FS 工具直接浏览。
    """

    backend_name = "folder"

    def __init__(self, root: Path, shard_size: int = 2) -> None:
        """`root` = 根目录;`shard_size` = 分片前缀长度(0 = 不分片,等同平铺)。"""
        self._root = Path(root)
        self._shard = shard_size  # 0 = 不分片,等同平铺

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
        """默认 `BytesIO(await get())`;Folder 不 override streaming。"""
        from io import BytesIO

        return BytesIO(await self.get(key))

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
                    # 重组:如果文件名带分片目录(head/tail/),合并成完整 key
                    # 例:`media / ab / cd / abc.jpg` → `media/abc.jpg`
                    if shard > 0 and len(parts) >= 4 and parts[-1].startswith(
                        parts[-3] + parts[-2] + "",
                    ):
                        # 最后一段文件名以 head + tail 开头 → 重组
                        # 反向:把 `parent / head / tail / name` → `parent / name`
                        # 这里 parent 是 parts[:-3]
                        parent = parts[:-3]
                        reconstructed = "/".join(parent + [parts[-1]])
                    elif shard > 0 and len(parts) >= 3 and parts[-1].startswith(
                        parts[-2] + "",
                    ) and len(parts[-2]) == shard:
                        # 三段式:parent / head / name(分片未生效 — 名字太短不分片)
                        reconstructed = "/".join(parts)
                    else:
                        reconstructed = "/".join(parts)
                    if prefix and not reconstructed.startswith(prefix):
                        continue
                    keys.append(reconstructed)
            return keys

        items = await asyncio.to_thread(_walk)
        for k in items:
            yield k
