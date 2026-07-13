"""LocalObjectStore 单测。"""
from __future__ import annotations

import pytest

from tgmonitor.core.objectstore.base import ObjectMeta
from tgmonitor.core.objectstore.local_store import LocalObjectStore


async def test_put_get_roundtrip(tmp_path):
    s = LocalObjectStore(root=tmp_path)
    await s.connect()
    await s.put("media/abc.jpg", b"hello", ObjectMeta(content_type="image/jpeg"))
    assert await s.exists("media/abc.jpg")
    assert await s.get("media/abc.jpg") == b"hello"


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
