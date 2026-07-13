"""FolderObjectStore 单测 — 两级分片。"""
from __future__ import annotations

from pathlib import Path

import pytest

from tgmonitor.core.objectstore.folder_store import FolderObjectStore


async def test_sharded_layout(tmp_path: Path):
    s = FolderObjectStore(root=tmp_path, shard_size=2)
    await s.connect()
    await s.put("media/abcdef1234567890.jpg", b"hello")
    # 路径: <root>/media/ab/cd/abcdef1234567890.jpg (按文件名分片,目录前缀保留)
    p = tmp_path / "media" / "ab" / "cd" / "abcdef1234567890.jpg"
    assert p.exists()
    assert await s.get("media/abcdef1234567890.jpg") == b"hello"


async def test_invalid_key_rejected(tmp_path: Path):
    s = FolderObjectStore(root=tmp_path)
    await s.connect()
    with pytest.raises(ValueError):
        await s.put("../etc/passwd", b"x")
    with pytest.raises(ValueError):
        await s.put("/abs/path", b"x")


async def test_delete_and_missing(tmp_path: Path):
    s = FolderObjectStore(root=tmp_path)
    await s.connect()
    await s.put("k", b"x")
    assert await s.exists("k")
    await s.delete("k")
    assert not await s.exists("k")
    with pytest.raises(KeyError):
        await s.get("k")


async def test_stat_size(tmp_path: Path):
    s = FolderObjectStore(root=tmp_path)
    await s.connect()
    await s.put("a", b"12345")
    meta = await s.stat("a")
    assert meta is not None
    assert meta.size == 5


async def test_short_key_no_shard(tmp_path: Path):
    s = FolderObjectStore(root=tmp_path, shard_size=2)
    await s.connect()
    await s.put("k", b"short")
    # 短 key 不分片
    assert (tmp_path / "k").exists()
    assert not (tmp_path / "k" / "k").exists()
