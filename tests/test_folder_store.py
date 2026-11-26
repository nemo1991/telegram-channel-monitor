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


# =========================================================================
# 2026-11-25 P2 批次(#20 / #21 / #27)
# =========================================================================


# ---- #20:旁车文件(.origkey)编码原始 key,iter_keys 优先还原 ----


async def test_iter_keys_roundtrip_after_put(tmp_path):
    """put → iter_keys 返回原 key(旁车还原;旧启发式 4-part 也命中,双保险)。"""
    s = FolderObjectStore(root=tmp_path, shard_size=2)
    await s.connect()
    await s.put("media/abcdef1234567890.jpg", b"hello")
    keys = [k async for k in s.iter_keys()]
    assert keys == ["media/abcdef1234567890.jpg"]


async def test_iter_keys_sidecar_restores_multi_level_subdir_key(tmp_path):
    """#20 回归(报告 #20 受害场景):多级子目录 key 不再被启发式产错。

    旧实现:key = "media/sub/abcdef1234567890.jpg" 落盘
    <root>/media/sub/ab/cd/abcdef1234567890.jpg(5 段路径),
    启发式 startswith 不命中 → 反推出错 key
    "media/sub/ab/cd/abcdef1234567890.jpg" → reconcile_orphans 误删。
    新实现:put 落盘写旁车 .origkey,iter_keys 读旁车还原原 key。
    """
    s = FolderObjectStore(root=tmp_path, shard_size=2)
    await s.connect()
    key = "media/sub/abcdef1234567890.jpg"
    await s.put(key, b"hello")

    obj = tmp_path / "media" / "sub" / "ab" / "cd" / "abcdef1234567890.jpg"
    sidecar = obj.with_suffix(obj.suffix + ".origkey")
    assert obj.exists()
    assert sidecar.read_text(encoding="utf-8") == key

    keys = [k async for k in s.iter_keys()]
    assert keys == [key], (
        f"多级子目录 key 必须经旁车还原,实际: {keys}"
        "(旧启发式会产出 media/sub/ab/cd/... 错 key → reconcile 误删)"
    )


async def test_iter_keys_prefix_filter_with_sidecar(tmp_path):
    """#20:prefix 过滤作用于还原后的原 key。"""
    s = FolderObjectStore(root=tmp_path, shard_size=2)
    await s.connect()
    await s.put("media/abcdef1234567890.jpg", b"a")
    await s.put("thumb/abcdef1234567890.jpg", b"b")
    keys = [k async for k in s.iter_keys(prefix="media/")]
    assert keys == ["media/abcdef1234567890.jpg"]


async def test_iter_keys_fallback_without_sidecar(tmp_path):
    """#20:旧数据(无旁车)fallback 启发式,行为与旧实现一致(向后兼容)。"""
    s = FolderObjectStore(root=tmp_path, shard_size=2)
    await s.connect()
    # 手工放置无旁车的分片布局文件(模拟旧版本落盘)
    legacy = tmp_path / "media" / "ab" / "cd" / "abcdef1234.jpg"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"x")
    keys = [k async for k in s.iter_keys()]
    assert keys == ["media/abcdef1234.jpg"]


async def test_delete_removes_object_and_sidecar(tmp_path):
    """#20:delete 同时清旁车 — 否则 iter_keys 会继续产出已删 key。"""
    s = FolderObjectStore(root=tmp_path, shard_size=2)
    await s.connect()
    await s.put("media/abcdef1234567890.jpg", b"hello")
    await s.delete("media/abcdef1234567890.jpg")
    sidecar = tmp_path / "media" / "ab" / "cd" / "abcdef1234567890.jpg.origkey"
    assert not sidecar.exists()
    keys = [k async for k in s.iter_keys()]
    assert keys == []


async def test_orphan_sidecar_ignored(tmp_path):
    """#20:孤儿旁车(对象文件已不在,crash 窗口)不产 key。"""
    s = FolderObjectStore(root=tmp_path, shard_size=2)
    await s.connect()
    orphan_sidecar = tmp_path / "media" / "ab" / "cd" / "abcdef.jpg.origkey"
    orphan_sidecar.parent.mkdir(parents=True, exist_ok=True)
    orphan_sidecar.write_text("media/abcdef.jpg", encoding="utf-8")
    keys = [k async for k in s.iter_keys()]
    assert keys == []


async def test_put_rejects_reserved_sidecar_suffix_key(tmp_path):
    """#20:对象 key 文件名不得占用旁车后缀(否则旁车识别二义)。"""
    s = FolderObjectStore(root=tmp_path, shard_size=2)
    await s.connect()
    with pytest.raises(ValueError):
        await s.put("media/abc.origkey", b"x")


# ---- #21:Folder put 的 sha256 回填(旧实现不算 → thumbnails.sha256 恒 NULL)----


async def test_folder_put_backfills_sha256(tmp_path):
    """#21 回归:Folder 后端 put 也自动补 sha256,与 Local 行为一致。"""
    import hashlib

    from tgmonitor.core.objectstore.base import ObjectMeta

    s = FolderObjectStore(root=tmp_path, shard_size=2)
    await s.connect()
    meta = ObjectMeta(content_type="image/jpeg")
    await s.put("media/abcdef1234567890.jpg", b"hello", meta)
    assert meta.sha256 == hashlib.sha256(b"hello").hexdigest()
    # caller 已给 sha256 → 不覆盖
    meta2 = ObjectMeta(sha256="given")
    await s.put("media/ffffffffffffffff.jpg", b"hello", meta2)
    assert meta2.sha256 == "given"


# ---- #27:Folder put_stream(旁车一并落盘)----


async def test_folder_put_stream_writes_object_and_sidecar(tmp_path):
    """#27:put_stream 逐块写 + 旁车 — iter_keys 能还原原 key。"""
    import hashlib

    s = FolderObjectStore(root=tmp_path, shard_size=2)
    await s.connect()

    async def _chunks():
        yield b"he"
        yield b"llo"

    meta = await s.put_stream("media/abcdef1234567890.jpg", _chunks())
    p = tmp_path / "media" / "ab" / "cd" / "abcdef1234567890.jpg"
    assert p.read_bytes() == b"hello"
    assert (
        p.with_suffix(p.suffix + ".origkey").read_text(encoding="utf-8")
        == "media/abcdef1234567890.jpg"
    )
    keys = [k async for k in s.iter_keys()]
    assert keys == ["media/abcdef1234567890.jpg"]
    assert meta.size == 5
    assert meta.sha256 == hashlib.sha256(b"hello").hexdigest()
