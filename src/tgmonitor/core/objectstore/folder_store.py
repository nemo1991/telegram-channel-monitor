"""FolderObjectStore — 本地 FS,两级分片布局。

布局:`<root>/<目录前缀>/<文件名前2位>/<文件名第3-4位>/<key>`
(目录前缀按字面保留,分片针对**文件名**部分) —— 例如
    key = "media/abcdef1234567890.jpg"
    落盘: <root>/media/ab/cd/abcdef1234567890.jpg

适用:大量小文件时,避免单目录 inode 压力;仍可用任何 FS 工具直接浏览。

2026-11-25 P1-N3:`connect/close/_ensure_inside_root/get/exists/delete/
stat/open_read/stream_read` 与 Local 后端逐字重复,已上提到 `_fs_base.py`
共享基类。

2026-11-25 #21:`put` 上提共享基类(sha256 回填两后端行为一致),
本类通过 `_post_put` 钩子写旁车。

2026-11-25 #20:落盘时写**旁车文件**编码原始 key(`<对象文件名>.origkey`,
内容 = 原 key UTF-8 文本)。`iter_keys` 优先读旁车还原原 key,没有旁车
的历史数据 fallback 走启发式反推。

背景:启发式对**多级子目录 key** 会产错 key —— 例:
    key = "media/sub/abcdef1234567890.jpg"
    落盘: <root>/media/sub/ab/cd/abcdef1234567890.jpg
    反推 parts = [media, sub, ab, cd, abcdef1234567890.jpg],5 段,
    `parts[-1].startswith(parts[-3]+parts[-2])` 不命中 → 掉进 else 返
    `media/sub/ab/cd/abcdef1234567890.jpg` — 错 key 被 `reconcile_orphans`
    当孤儿删,真正对象反而 dangling。旁车方案彻底消除这类反推错误。
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import AsyncIterator

from tgmonitor.core.objectstore._fs_base import LocalFsStoreBase

_VALID_KEY = re.compile(r"^[A-Za-z0-9_./\-]+$")

# 旁车文件后缀(#20):`<对象文件名>.origkey`,内容 = 原始 key。
# 选后缀而非独立 `.key` 文件名:与对象文件同目录同名,对应关系一目
# 了然,且不改变分片目录结构、不打扰"目录 = 分片"的列目录语义。
_SIDECAR_SUFFIX = ".origkey"


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
        name = key.rpartition("/")[2]
        if name.endswith(_SIDECAR_SUFFIX):
            # 保留后缀:文件名以 .origkey 结尾会让 iter_keys 的旁车识别
            # 无法区分「对象文件」与「旁车文件」。make_key 产 sha256 名,
            # 正常路径永不触发。
            raise ValueError(
                f"object key filename may not use reserved sidecar suffix "
                f"{_SIDECAR_SUFFIX!r}: {key!r}"
            )
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

    # ---- 旁车(#20)----

    def _sidecar_path(self, object_path: Path) -> Path:
        """对象落盘路径 → 旁车文件路径(同目录,后缀 `.origkey`)。"""
        return object_path.with_name(object_path.name + _SIDECAR_SUFFIX)

    def _post_put(self, key: str, path: Path) -> None:
        """落盘后写旁车编码原始 key(在 put 的 worker 线程内被调)。

        非原子写:crash 窗口内旁车缺失 → iter_keys fallback 启发式,
        与旧数据行为一致(降级而非损坏)。
        """
        self._sidecar_path(path).write_text(key, encoding="utf-8")

    async def delete(self, key: str) -> None:
        """删除对象 + 旁车(#20);不存在不抛(idempotent)。"""
        path = self._path(key)
        self._ensure_inside_root(path)
        await super().delete(key)
        sidecar = self._sidecar_path(path)
        await asyncio.to_thread(lambda: sidecar.unlink(missing_ok=True))

    # ---- 操作 ----

    async def iter_keys(self, prefix: str = "") -> AsyncIterator[str]:
        """枚举所有 key — 把分片布局展开成原始 key(2026-08-24 orphan reconcile)。

        2026-11-25 #20:优先读旁车(`<对象文件名>.origkey`)还原原 key;
        无旁车的历史数据 fallback 启发式反推。旁车识别规则:文件名以
        `.origkey` 结尾**且**剥掉后缀后对应对象文件存在(两遍扫描,先收集
        全部文件名再分类,避免「对象文件名本身以 .origkey 结尾」时误判)。

        走 to_thread 防 UI 卡顿。
        """
        root = self._root
        shard = self._shard

        def _reconstruct(parts: list[str]) -> str:
            """启发式:把分片布局路径段反推回原始 key(无旁车时 fallback)。"""
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
                return "/".join(parts[:-3] + [parts[-1]])
            if shard > 0 and len(parts) == 3:
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
                return parts[-1]
            return "/".join(parts)

        def _walk() -> tuple[list[str], dict[str, str]]:
            """一遍 os.walk:返 (对象文件 rel 路径列表, 对象 rel → 原始 key 映射)。"""
            files: list[str] = []
            for dirpath, _dirnames, filenames in os.walk(root):
                for fn in filenames:
                    if ".tgmonitor_write_probe" in fn:
                        continue
                    full = Path(dirpath) / fn
                    files.append(full.relative_to(root).as_posix())
            file_set = set(files)
            objects: list[str] = []
            sidecars: dict[str, str] = {}
            for rel in files:
                if not rel.endswith(_SIDECAR_SUFFIX):
                    objects.append(rel)
                    continue
                obj_rel = rel[: -len(_SIDECAR_SUFFIX)]
                if obj_rel not in file_set:
                    # 孤儿旁车(对象文件已不在,crash 窗口)→ 忽略
                    continue
                try:
                    sidecars[obj_rel] = (root / rel).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    # 读失败(权限 / 编码损坏)→ 当无旁车,fallback 启发式
                    continue
            return objects, sidecars

        objects, sidecars = await asyncio.to_thread(_walk)
        for rel in objects:
            # SIM108:三元式(ruff 要求);旁车优先,无旁车 fallback 启发式
            reconstructed = sidecars[rel] if rel in sidecars else _reconstruct(rel.split("/"))
            if prefix and not reconstructed.startswith(prefix):
                continue
            yield reconstructed
