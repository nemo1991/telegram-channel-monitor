"""MediaDownloader 真下载测试 — REVIEW M2.1 接入测试。

走 `FakeTelegramClient.download_file` + `LocalObjectStore(tmp_path)`,
不依赖 TDLib native,纯内存 + tmpfs 可跑。

覆盖:
  1. 成功:注入 bytes → ObjectStore 真写入 → 返回的 MediaDTO 填了 object_key
     且 `download_status=DONE`
  2. file_id 缺失 → FAILED + "无 telegram_file_id" + 不抛
  3. file_size > max_bytes(known-size)→ FAILED + 不下载
  4. max_bytes=0 → 已知大尺寸也不拦
  5. download_file 返 None → FAILED + 不抛
  6. 真下载 > max_bytes(unknown-size hard cap)→ FAILED + 不写入对象存储
  7. make_key 稳定性:同一 file_id 不同 file_name → 同 key
  8. storage 已有同 file_id DONE → skip #1(2026-08-24)
  9. ObjectStore 已有同 key → skip #2(2026-08-24)
 10. 两个 skip 都没命中 → 走原下载路径
 11. force=True 绕过 skip #1 — 真重下覆盖(2026-08-24 Media Manager retry)
 12. force=True 绕过 skip #2 — 真重下覆盖
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MediaType
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


@pytest.fixture
def storage():
    """2026-08-24:download_one 现在依赖 storage.find_media_by_file_id,所有
    测试用 InMemoryRepository 替原 None。"""
    from tests.conftest import InMemoryRepository

    return InMemoryRepository()


def _make_dl(
    client: FakeTelegramClient,
    objects: LocalObjectStore,
    storage,
    **kw,
) -> MediaDownloader:
    return MediaDownloader(client, storage, objects, **kw)


# ---- 1. 成功路径 ----


async def test_download_one_stores_bytes_and_returns_updated_dto(
    client: FakeTelegramClient, objects: LocalObjectStore, storage
) -> None:
    # 精确字节数:8 字节 PNG 头 + 110 字节 payload = 118
    payload = b"\x89PNG\r\n\x1a\n" + b"fakepayload" * 10  # 118 bytes
    client.set_download("fid-A", payload)
    dl = _make_dl(client, objects, storage)

    out = await dl.download_one(msg_pk=42, media=_make_media(file_size=118))

    assert out is not None, "expected updated MediaDTO, got None"
    assert out.download_status == MediaDownloadStatus.DONE
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


async def test_download_one_returns_failed_when_file_id_missing(
    client: FakeTelegramClient, objects: LocalObjectStore, storage
) -> None:
    dl = _make_dl(client, objects, storage)
    med = _make_media(telegram_file_id=None)

    out = await dl.download_one(msg_pk=1, media=med)

    assert out is not None
    assert out.download_status == MediaDownloadStatus.FAILED
    assert out.download_error, "失败应带原因"
    assert out.object_key is None
    # 没 file_id → 注入啥都没用
    client.set_download("fid-A", b"data")
    assert await client.download_file("fid-A") == b"data"
    # 确认 objects 没被写
    assert await objects.exists(MediaDownloader.make_key(med)) is False


# ---- 3. 已知 oversized(被 settings 拒)----


async def test_download_one_skips_oversized_by_setting(
    client: FakeTelegramClient, objects: LocalObjectStore, storage
) -> None:
    client.set_download("fid-A", b"X" * 100)
    dl = _make_dl(client, objects, storage, max_bytes=200_000_000)
    med = _make_media(file_size=300_000_000)  # 300 MB > 200 MB cap

    out = await dl.download_one(msg_pk=2, media=med)

    assert out is not None, "300MB > 200MB 应被 max_bytes 拦截"
    assert out.download_status == MediaDownloadStatus.FAILED
    assert out.download_error
    # 没写
    assert not (objects._root / "media").exists() or not any((objects._root / "media").iterdir())


# ---- 4. max_bytes=0 = 无限制(已知大尺寸也通过)----


async def test_download_one_zero_max_bytes_means_unlimited(
    client: FakeTelegramClient, objects: LocalObjectStore, storage
) -> None:
    payload = b"Z" * 500
    client.set_download("fid-A", payload)
    dl = _make_dl(client, objects, storage, max_bytes=0)
    med = _make_media(file_size=10**12)  # 1 TB,但 max_bytes=0 不拦

    out = await dl.download_one(msg_pk=3, media=med)

    assert out is not None
    assert out.download_status == MediaDownloadStatus.DONE
    assert out.file_size == 500  # 真下载 500 bytes
    assert await objects.get(out.object_key) == payload


# ---- 5. download 失败 ----


async def test_download_one_returns_failed_on_download_failure(
    client: FakeTelegramClient, objects: LocalObjectStore, storage
) -> None:
    client.set_download("fid-A", None)  # 显式注入 None = 失败
    dl = _make_dl(client, objects, storage)

    out = await dl.download_one(msg_pk=4, media=_make_media())

    assert out is not None
    assert out.download_status == MediaDownloadStatus.FAILED
    assert out.download_error, "失败应带原因"
    assert out.object_key is None
    # 没有任何 bytes 写入
    assert not (objects._root / "media").exists() or not any((objects._root / "media").iterdir())


# ---- 6. unknown-size hard cap(已知 file_size=None,真下来超大)----


async def test_download_one_hard_cap_for_unknown_size(
    client: FakeTelegramClient, objects: LocalObjectStore, storage
) -> None:
    """file_size 未知(媒体类型不报大小,如某些 sticker),但真下来 > max_bytes → 拒。

    验证:
      - 返 FAILED + download_error(不返 object_key 写过的 DTO)
      - `objects.put` 未被调用(检查在 put 之前完成),对象存储无残留文件
    """
    payload = b"BIG" * 100_000  # 300 KB
    client.set_download("fid-A", payload)
    dl = _make_dl(client, objects, storage, max_bytes=1000)  # 1 KB 上限

    out = await dl.download_one(
        msg_pk=5,
        media=_make_media(file_size=None),  # 大小未知
    )

    assert out is not None
    assert out.download_status == MediaDownloadStatus.FAILED
    assert out.download_error, "失败应带原因"
    assert out.object_key is None
    # 确认 objects.put 没被调用(否则返 DONE 的 DTO)
    assert not (objects._root / "media").exists() or not any((objects._root / "media").iterdir())


# ---- 7. make_key 稳定性(同一 file_id 不同 file_name → 同 key)----


def test_make_key_is_stable_across_file_name(
    client: FakeTelegramClient, objects: LocalObjectStore, storage
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
    client: FakeTelegramClient, objects: LocalObjectStore, storage
) -> None:
    payload = b"X" * 5000  # 5 KB
    client.set_download("fid-A", payload)
    dl = _make_dl(client, objects, storage, max_bytes=0)
    med = _make_media(file_size=None)  # 未知

    out = await dl.download_one(msg_pk=6, media=med)

    assert out is not None
    assert out.download_status == MediaDownloadStatus.DONE
    assert out.file_size == 5000
    assert await objects.get(out.object_key) == payload


# ---- ObjectMeta 透传 ----


async def test_download_one_passes_size_via_meta(
    client: FakeTelegramClient, objects: LocalObjectStore, storage
) -> None:
    """LocalObjectStore 自动算 sha256(若 meta.sha256 is None),size 由 stat 返。

    注:`LocalObjectStore.stat()` 只返 `size`(从文件 stat),不持久化 content_type
    — 它是无 sidecar 的纯 FS 实现。MediaDownloader 仍按 Protocol 约定 put
    ObjectMeta(content_type=...) 进去(LocalObjectStore.put 接受但不存);其它
    backend(s3)会持久化。这条测试只断言 size + sha256 自动算。
    """
    payload = b"abc123"
    client.set_download("fid-A", payload)
    dl = _make_dl(client, objects, storage)

    out = await dl.download_one(
        msg_pk=7,
        media=_make_media(mime_type="application/octet-stream", file_size=6),
    )

    assert out is not None
    assert out.download_status == MediaDownloadStatus.DONE
    meta = await objects.stat(out.object_key)
    assert meta is not None
    assert meta.size == 6
    # 真下载下来 size 跟 file_size 对齐
    assert out.file_size == 6


# ============================================================
# 2026-08-24:skip-if-stored(skip #1 storage / skip #2 objectstore / 落空路径)
# ============================================================


async def test_download_one_skips_when_storage_has_object_key(
    client: FakeTelegramClient, objects: LocalObjectStore, storage
) -> None:
    """skip #1:storage 已有同 file_id 的 DONE media → 拷字段,client 不调。"""
    import dataclasses
    from datetime import datetime as _dt

    from tgmonitor.core.dto import MessageDTO

    prior = _make_media(
        file_size=2048,
        mime_type="image/png",
        file_name="prior.png",
    )
    # pre-populate storage:同一 telegram_file_id="fid-A" 已经 DOWNLOADED,
    # 但用不同的 file_name 模拟跨消息共享 file_id 的场景。
    await storage.save_message(
        MessageDTO(
            id=0,
            channel_id=99,
            telegram_msg_id=1,
            text="prior",
            date=_dt(2026, 1, 1),
            media=[
                dataclasses.replace(
                    prior,
                    object_key="media/prior_key.png",
                    object_backend="local",
                    download_status=MediaDownloadStatus.DONE,
                )
            ],
        )
    )
    # 没在 client 注入任何 payload — 若走了 client 路径会返 None,失败。
    dl = _make_dl(client, objects, storage)
    med = _make_media(file_name="new_name.jpg", file_size=1024)

    out = await dl.download_one(msg_pk=2, media=med)

    assert out is not None
    assert out.download_status == MediaDownloadStatus.DONE
    assert out.object_key == "media/prior_key.png"
    assert out.object_backend == "local"
    assert out.file_size == 2048  # 从 prior 拷过来
    # 原 med.file_name 保留(只是 object_key 字段被覆盖)
    assert out.file_name == "new_name.jpg"


async def test_download_one_skips_when_objectstore_has_key(
    client: FakeTelegramClient, objects: LocalObjectStore, storage
) -> None:
    """skip #2:ObjectStore 已有同 key(且 storage 查不到)→ 视为已下载。"""
    med = _make_media(file_size=1024)
    key = MediaDownloader.make_key(med)
    # pre-put bytes 进 ObjectStore(模拟之前下过)
    await objects.put(key, b"already-there", None)
    # client 不注入任何 payload — 若走了 client 路径会 None 失败,
    # 所以测试覆盖 skip #2 真的跳过 client
    dl = _make_dl(client, objects, storage)

    out = await dl.download_one(msg_pk=3, media=med)

    assert out is not None
    assert out.download_status == MediaDownloadStatus.DONE
    assert out.object_key == key
    assert out.object_backend == "local"
    # file_size 在 skip #2 路径上保留原 med.file_size(prior.file_size 不可知)
    assert out.file_size == 1024


async def test_download_one_falls_through_when_no_skip(
    client: FakeTelegramClient, objects: LocalObjectStore, storage
) -> None:
    """storage 空 + ObjectStore 空 + client 注入 payload → 走正常下载路径。"""
    payload = b"realfresh"
    client.set_download("fid-A", payload)
    dl = _make_dl(client, objects, storage)
    med = _make_media(file_size=len(payload))

    out = await dl.download_one(msg_pk=4, media=med)

    assert out is not None
    assert out.download_status == MediaDownloadStatus.DONE
    # object_key 应是新生成的(与 storage 里的不同,因为 storage 空)
    assert out.object_key
    assert out.object_key.startswith("media/")
    # 字节真落盘
    assert await objects.get(out.object_key) == payload


# ============================================================
# 2026-08-24 Media Manager:retry 路径 force=True 绕过 skip #1 / #2
# ============================================================


async def test_download_one_force_bypasses_storage_skip(
    client: FakeTelegramClient, objects: LocalObjectStore, storage
) -> None:
    """retry:storage 已有同 file_id DONE,force=True → 不拷字段,真重下覆盖。

    预置:storage 里 (99, 1) 的 media 已 DONE,object_key="media/prior.png"。
    调用:download_one(force=True) → 应走到 client.download_file → 落新 bytes。
    断言:object_key 不再是 prior(可能是同 key 但内容已变);bytes 真更新到新注入的 payload。
    """
    import dataclasses

    from tgmonitor.core.dto import MessageDTO

    prior = _make_media(
        file_size=2048,
        mime_type="image/png",
        file_name="prior.png",
    )
    await storage.save_message(
        MessageDTO(
            id=0,
            channel_id=99,
            telegram_msg_id=1,
            text="prior",
            date=datetime.now(UTC),  # type: ignore[arg-type]
            media=[
                dataclasses.replace(
                    prior,
                    object_key="media/prior.png",
                    object_backend="local",
                    download_status=MediaDownloadStatus.DONE,
                )
            ],
        )
    )
    new_payload = b"newly-fetched"
    client.set_download("fid-A", new_payload)
    dl = _make_dl(client, objects, storage)

    out = await dl.download_one(msg_pk=2, media=_make_media(), force=True)

    assert out is not None
    assert out.download_status == MediaDownloadStatus.DONE
    assert out.object_key, "force 路径必须真重下,object_key 应填"
    # 新 bytes 落盘,且 key 与 prior 不同(make_key 用 file_name 算 hash,但 fid 一样
    # 时 hash 一样 → key 会相同;force 路径重点是覆盖,key 同说明写入覆盖了 prior)。
    # 关键是 bytes 是新注入的,不是 storage 旧 2048 字节。
    assert await objects.get(out.object_key) == new_payload


async def test_download_one_force_bypasses_objectstore_skip(
    client: FakeTelegramClient, objects: LocalObjectStore, storage
) -> None:
    """retry:ObjectStore 已有同 key(且 storage 查不到),force=True → 重新下载覆盖。

    预置:objects.put(key, b"old-data")。
    调用:download_one(force=True) → 应走到 client.download_file → 写入新 payload。
    断言:objects 真有被覆盖为新 bytes。
    """
    med = _make_media()
    key = MediaDownloader.make_key(med)
    await objects.put(key, b"old-data", None)
    new_payload = b"fresh-bytes"
    client.set_download("fid-A", new_payload)
    dl = _make_dl(client, objects, storage)

    out = await dl.download_one(msg_pk=3, media=med, force=True)

    assert out is not None
    assert out.download_status == MediaDownloadStatus.DONE
    assert out.object_key == key
    # bytes 应是新 payload,不是 old-data
    assert await objects.get(key) == new_payload
    # 不走 skip → file_size 应是新下载的真实大小
    assert out.file_size == len(new_payload)
