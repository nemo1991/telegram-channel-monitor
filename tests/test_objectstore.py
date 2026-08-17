"""对象存储后端单测(Local + Folder)。"""
from __future__ import annotations

import asyncio

import pytest

from tgmonitor.core.objectstore.base import ObjectMeta
from tgmonitor.core.objectstore.folder_store import FolderObjectStore
from tgmonitor.core.objectstore.local_store import LocalObjectStore


async def _wrap_to_thread(monkeypatch, calls: list[str]) -> None:
    """把 asyncio.to_thread 包一层:记录被调度函数名,再照常执行。"""

    original = asyncio.to_thread

    async def recording(fn, *args, **kwargs):
        calls.append(getattr(fn, "__name__", repr(fn)))
        return await original(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", recording)


# ---- Local ----

async def test_put_get_roundtrip(tmp_path):
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    await s.put("media/abc.jpg", b"hello", ObjectMeta(content_type="image/jpeg"))
    assert await s.exists("media/abc.jpg")
    assert await s.get("media/abc.jpg") == b"hello"


async def test_put_get_uses_to_thread(tmp_path, monkeypatch):
    """写盘/读盘必须走 asyncio.to_thread,避免大文件 IO 阻塞事件循环。"""
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    calls: list[str] = []
    await _wrap_to_thread(monkeypatch, calls)
    data = b"x" * (1024 * 1024)  # 1MB
    await s.put("media/big.bin", data, ObjectMeta(content_type="application/octet-stream"))
    assert await s.get("media/big.bin") == data
    assert any("write" in c for c in calls), f"put 未走 to_thread: {calls}"
    assert "read_bytes" in calls, f"get 未走 to_thread: {calls}"


async def test_delete(tmp_path):
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    await s.put("k", b"x")
    await s.delete("k")
    assert not await s.exists("k")


async def test_path_traversal_rejected(tmp_path):
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    with pytest.raises(ValueError):
        await s.put("../etc/passwd", b"x")
    with pytest.raises(ValueError):
        await s.put("/abs", b"x")


async def test_stat(tmp_path):
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    await s.put("k", b"12345")
    meta = await s.stat("k")
    assert meta is not None
    assert meta.size == 5


async def test_get_missing_raises(tmp_path):
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    with pytest.raises(KeyError):
        await s.get("nope")


# ---- Folder(两级分片)----

async def test_folder_put_get_roundtrip(tmp_path):
    s = FolderObjectStore(root=tmp_path)
    await s.connect()
    await s.put("media/abcdef.jpg", b"hello", ObjectMeta(content_type="image/jpeg"))
    assert await s.exists("media/abcdef.jpg")
    # 两级分片:media/ab/cd/abcdef.jpg
    assert (tmp_path / "media" / "ab" / "cd" / "abcdef.jpg").exists()
    assert await s.get("media/abcdef.jpg") == b"hello"


async def test_folder_shard_zero_is_flat(tmp_path):
    s = FolderObjectStore(root=tmp_path, shard_size=0)
    await s.connect()
    await s.put("media/abcdef.jpg", b"hello")
    assert (tmp_path / "media" / "abcdef.jpg").exists()


async def test_folder_put_get_uses_to_thread(tmp_path, monkeypatch):
    """Folder 后端写盘/读盘同样必须走 asyncio.to_thread。"""
    s = FolderObjectStore(root=tmp_path)
    await s.connect()
    calls: list[str] = []
    await _wrap_to_thread(monkeypatch, calls)
    data = b"x" * (1024 * 1024)  # 1MB
    await s.put("media/big.bin", data)
    assert await s.get("media/big.bin") == data
    assert any("write" in c for c in calls), f"put 未走 to_thread: {calls}"
    assert "read_bytes" in calls, f"get 未走 to_thread: {calls}"
