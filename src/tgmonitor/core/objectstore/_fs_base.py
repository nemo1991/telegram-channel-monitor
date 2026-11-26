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
import os
import secrets
import time
from abc import abstractmethod
from pathlib import Path
from typing import AsyncIterator, BinaryIO

from tgmonitor.core.objectstore.base import ObjectMeta, ObjectStore, probe_writable

# `_replace_atomic` 的重试节奏:Windows 上目标刚被另一个写者替换掉时,
# 旧文件对象短暂处于 delete-pending,此刻再替换会得到 ACCESS_DENIED;
# 通常几毫秒内释放。
_REPLACE_ATTEMPTS = 6
_REPLACE_DELAY_S = 0.01


def _without_extended_prefix(path: Path) -> Path:
    """去掉 Windows 长路径前缀 `\\\\?\\`(`\\\\?\\UNC\\server\\share` → `\\\\server\\share`)。

    只做字面规范化,不做任何解析 —— 调用方用它来比较两个路径是否同一位置。
    """
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + text[len("\\\\?\\UNC\\") :])
    if text.startswith("\\\\?\\"):
        return Path(text[len("\\\\?\\") :])
    return path


def _replace_atomic(src: Path, dst: Path) -> None:
    """`os.replace` + Windows 短暂拒绝的重试(2.1.7)。

    同一目标被两个写者先后替换(并发下载同一媒体)时,Windows 上后一个会
    撞上前一个留下的 delete-pending 旧文件,得到 `PermissionError`。
    这是瞬时状态,退避重试即可;持续失败(真权限问题)照常上抛 —— 对象本体
    由前一个写者完整写入,不会留下半成品。
    """
    delay = _REPLACE_DELAY_S
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay *= 2


def _mkdir_parents(path: Path) -> None:
    """`mkdir(parents=True, exist_ok=True)` + Windows 短暂拒绝的重试。

    两个线程同时创建同一个新目录时,Windows 对后来者返回 ACCESS_DENIED 而
    不是 ALREADY_EXISTS,此时目录尚不可见,`exist_ok` 也兜不住。瞬时状态,
    退避重试后目录已就位,`exist_ok` 生效。
    """
    delay = _REPLACE_DELAY_S
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            path.mkdir(parents=True, exist_ok=True)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay *= 2


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
        root = self._root.resolve()
        try:
            path.relative_to(root)
            return
        except ValueError as e:
            # Windows:父目录正被另一线程创建的那个瞬间,`Path.resolve()` 可能
            # 返回长路径形式 `\\?\C:\...`,与 root 的短路径形式字面不等,但
            # 指向同一位置(2.1.7:并发首次写入同一目录时踩到)。去掉前缀再比
            # 一次;仍不在 root 内才判越界。`\\?\` 形式不接受 `..` 与 `/`,
            # 规范化只会消掉这处字面差异,不会放宽越界判定。
            if os.name == "nt":
                try:
                    _without_extended_prefix(path).relative_to(_without_extended_prefix(root))
                    return
                except ValueError:
                    pass
            raise ValueError(f"key escapes root: {path}") from e

    def local_path(self, key: str) -> Path | None:
        """P1-C3 能力:本地 FS 后端都有真实落盘路径 → 委托 `_path()`。"""
        return self._path(key)

    # ---- 写入(Local / Folder 共用,#21 / #27)----

    # 写入中的临时文件名前缀:`iter_keys` 据此跳过,不把半成品当对象。
    # 合法对象的文件名是 `<hash>.<ext>`(见 `MediaDownloader.make_key`),
    # 不会以 `.tgmonitor-` 开头 —— 不能只按 `.part` 后缀过滤:远端文件
    # 完全可以叫 `video.mp4.part`,那样对象会被当成孤儿误删。
    _TMP_PREFIX = ".tgmonitor-"

    @classmethod
    def _tmp_path_for(cls, path: Path) -> Path:
        """同目录唯一临时名 `.tgmonitor-<12 位随机十六进制><ext>.part`。

        2026-11-26 2.1.7:此前是固定的 `<name>.part` —— 同一 key 并发写
        (monitor 下载 worker 与手动下载 / 全量同步撞车)时两个写者互相
        截断。随机后缀让每个写者各写各的,`replace` 仍然原子,后完成者胜。
        不用 `tempfile.mkstemp`:它以 0600 建文件,会让落盘对象权限偏离
        umask 默认值;这里按常规 `open("wb")` 创建,权限行为不变。
        """
        return path.with_name(f"{cls._TMP_PREFIX}{secrets.token_hex(6)}{path.suffix}.part")

    @staticmethod
    def _write_bytes_atomic(path: Path, data: bytes, hash_needed: bool) -> str | None:
        """原子写:.part + rename;`hash_needed` 时顺手算 sha256。

        写盘 + sha256 是重 IO/CPU,由调用方放 `asyncio.to_thread` —
        否则 FULL 策略下下载 100MB+ 视频同步写盘会阻塞 qasync 主事件
        循环,UI 卡顿。中途失败清掉 .part 半成品,对象本体不出现。
        """
        _mkdir_parents(path.parent)
        tmp = LocalFsStoreBase._tmp_path_for(path)
        try:
            tmp.write_bytes(data)
            _replace_atomic(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
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

        def _open_tmp() -> tuple[BinaryIO, Path]:
            _mkdir_parents(path.parent)
            tmp = self._tmp_path_for(path)
            return tmp.open("wb"), tmp

        f, tmp = await asyncio.to_thread(_open_tmp)
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
            await asyncio.to_thread(_replace_atomic, tmp, path)
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
