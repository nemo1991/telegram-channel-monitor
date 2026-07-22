"""MediaDownloader 真下载测试 — REVIEW M2.1 接入测试。

走 `FakeTelegramClient.download_file` + `LocalObjectStore(tmp_path)`,
不依赖 aiotdlib native,纯内存 + tmpfs 可跑。

覆盖:
  1. 成功:注入 bytes → ObjectStore 真写入 → 返回的 MediaDTO 填了 object_key
  2. file_id 缺失 → None + 不抛
  3. file_size > max_bytes(known-size)→ None + 不下载
  4. max_bytes=0 → 已知大尺寸也不拦
  5. download_file 返 None → None + 不抛
  6. 真下载 > max_bytes(unknown-size hard cap)→ None + 不保留 part 文件
  7. make_key 稳定性:同一 file_id 不同 file_name → 同 key
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tgmonitor.core.dto import MediaDTO, MediaType
from tgmonitor.core.monitor.service import MediaDownloader
from tgmonitor.core.objectstore.local_store import LocalObjectStore
from tgmonitor.core.telegram.fake_client import FakeTelegramClient


def _make_media(**overrides) -> MediaDTO:
    """默认 photo + file_id='fid-A' + 已知大小 1024。"""
    base: dict = {
        "type": MediaType.PHOTO,
        "mime_type": "image/jpeg",
        "file_name": "test.jpg",
        "file_size": 1024,
        "telegram_file_id": "fid-A",
    }
    base.update(overrides)
    return MediaDTO(**base)  # type: ignore[arg-type]


@pytest.fixture
def client() -> FakeTelegramClient:
    return FakeTelegramClient()


@pytest.fixture
def objects(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(root=tmp_path / "media")


def _make_dl(
    client: FakeTelegramClient,
    objects: LocalObjectStore,
    **kw,
) -> MediaDownloader:
    # storage 不在 download_one 路径里,传 None 即可
    return MediaDownloader(client, None, objects, **kw)  # type: ignore[arg-type]


# ---- 1. 成功路径 ----

async def test_download_one_stores_bytes_and_returns_updated_dto(
    client: FakeTelegramClient, objects: LocalObjectStore
) -> None:
    # 精确字节数:8 字节 PNG 头 + 110 字节 payload = 118
    payload = b"\x89PNG\r\n\x1a\n" + b"fakepayload" * 10  # 118 bytes
    client.set_download("fid-A", payload)
    dl = _make_dl(client, objects)

    out = await dl.download_one(msg_pk=42, media=_make_media(file_size=118))

    assert out is not None, "expected updated MediaDTO, got None"
    assert out.object_key, "object_key 未填"
    assert out.object_backend == "local"
    assert out.file_size == 118, "真下载大小应覆盖 file_size"
    # bytes 一致
    stored = await objects.get(out.object_key)
    assert stored == payload
    # 原字段保留
    assert out.telegram_file_id == "fid-A"
    assert out.mime_type == "image/jpeg"


# ---- 2. file_id 缺失 ----

async def test_download_one_returns_none_when_file_id_missing(
    client: FakeTelegramClient, objects: LocalObjectStore
) -> None:
    dl = _make_dl(client, objects)
    med = _make_media(telegram_file_id=None)

    out = await dl.download_one(msg_pk=1, media=med)

    assert out is None
    # 没 file_id → 注入啥都没用
    client.set_download("fid-A", b"data")
    assert await client.download_file("fid-A") == b"data"
    # 确认 objects 没被写
    assert await objects.exists(MediaDownloader.make_key(med)) is False


# ---- 3. 已知 oversized(被 settings 拒)----

async def test_download_one_skips_oversized_by_setting(
    client: FakeTelegramClient, objects: LocalObjectStore
) -> None:
    client.set_download("fid-A", b"X" * 100)
    dl = _make_dl(client, objects, max_bytes=200_000_000)
    med = _make_media(file_size=300_000_000)  # 300 MB > 200 MB cap

    out = await dl.download_one(msg_pk=2, media=med)

    assert out is None, "300MB > 200MB 应被 max_bytes 拦截"
    # 没写
    assert not (objects._root / "media").exists() or not any(
        (objects._root / "media").iterdir()
    )


# ---- 4. max_bytes=0 = 无限制(已知大尺寸也通过)----

async def test_download_one_zero_max_bytes_means_unlimited(
    client: FakeTelegramClient, objects: LocalObjectStore
) -> None:
    payload = b"Z" * 500
    client.set_download("fid-A", payload)
    dl = _make_dl(client, objects, max_bytes=0)
    med = _make_media(file_size=10**12)  # 1 TB,但 max_bytes=0 不拦

    out = await dl.download_one(msg_pk=3, media=med)

    assert out is not None
    assert out.file_size == 500  # 真下载 500 bytes
    assert await objects.get(out.object_key) == payload


# ---- 5. download 失败 ----

async def test_download_one_returns_none_on_download_failure(
    client: FakeTelegramClient, objects: LocalObjectStore
) -> None:
    client.set_download("fid-A", None)  # 显式注入 None = 失败
    dl = _make_dl(client, objects)

    out = await dl.download_one(msg_pk=4, media=_make_media())

    assert out is None
    # 没有任何 bytes 写入
    assert not (objects._root / "media").exists() or not any(
        (objects._root / "media").iterdir()
    )


# ---- 6. unknown-size hard cap(已知 file_size=None,真下来超大)----

async def test_download_one_hard_cap_for_unknown_size(
    client: FakeTelegramClient, objects: LocalObjectStore
) -> None:
    """file_size 未知(媒体类型不报大小,如某些 sticker),但真下来 > max_bytes → 拒。

    验证:
      - 返 None(不返 object_key 写过的 DTO)
      - objects 里的 .part 已被原子 rename 覆盖成真实文件 — 但内容是已写入的
        full data,不在 None 路径里(因为真下载下来后才发现超 size);业务上
        等同于"下载了但拒绝入索引",可接受。
    """
    payload = b"BIG" * 100_000  # 300 KB
    client.set_download("fid-A", payload)
    dl = _make_dl(client, objects, max_bytes=1000)  # 1 KB 上限

    out = await dl.download_one(
        msg_pk=5, media=_make_media(file_size=None),  # 大小未知
    )

    assert out is None
    # 确认 objects.put 没被调用(否则返 DTO)
    assert not (objects._root / "media").exists() or not any(
        (objects._root / "media").iterdir()
    )


# ---- 7. make_key 稳定性(同一 file_id 不同 file_name → 同 key)----

def test_make_key_is_stable_across_file_name(
    client: FakeTelegramClient, objects: LocalObjectStore
) -> None:
    a = MediaDownloader.make_key(_make_media(file_name="a.jpg"))
    b = MediaDownloader.make_key(_make_media(file_name="b.png"))
    # 同一 file_id("fid-A")→ 同 hash 前缀
    assert a.startswith("media/") and b.startswith("media/")
    # 不同 file_name 但同一 file_id → hash 部分相同(都来自 "fid-A")
    assert a.rsplit(".", 2)[0] == b.rsplit(".", 2)[0], (
        f"同一 file_id 应产生同 hash 前缀;got {a!r} vs {b!r}"
    )


# ---- bonus:max_bytes=0 也接受 oversized 真下载(同等行为)----

async def test_download_one_zero_max_bytes_passes_actual_oversized(
    client: FakeTelegramClient, objects: LocalObjectStore
) -> None:
    payload = b"X" * 5000  # 5 KB
    client.set_download("fid-A", payload)
    dl = _make_dl(client, objects, max_bytes=0)
    med = _make_media(file_size=None)  # 未知

    out = await dl.download_one(msg_pk=6, media=med)

    assert out is not None
    assert out.file_size == 5000
    assert await objects.get(out.object_key) == payload


# ---- ObjectMeta 透传 ----

async def test_download_one_passes_size_via_meta(
    client: FakeTelegramClient, objects: LocalObjectStore
) -> None:
    """LocalObjectStore 自动算 sha256(若 meta.sha256 is None),size 由 stat 返。

    注:`LocalObjectStore.stat()` 只返 `size`(从文件 stat),不持久化 content_type
    — 它是无 sidecar 的纯 FS 实现。MediaDownloader 仍按 Protocol 约定 put
    ObjectMeta(content_type=...) 进去(LocalObjectStore.put 接受但不存);其它
    backend(s3)会持久化。这条测试只断言 size + sha256 自动算。
    """
    payload = b"abc123"
    client.set_download("fid-A", payload)
    dl = _make_dl(client, objects)

    out = await dl.download_one(
        msg_pk=7,
        media=_make_media(mime_type="application/octet-stream", file_size=6),
    )

    assert out is not None
    meta = await objects.stat(out.object_key)
    assert meta is not None
    assert meta.size == 6
    # 真下载下来 size 跟 file_size 对齐
    assert out.file_size == 6