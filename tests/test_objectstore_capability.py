"""ObjectStore 能力接口契约测试 — 2026-11-25 P1-C3。

证明核心命题:**新增第 4 种 ObjectStore 后端零改 core 可用**。
老实现用 `isinstance(LocalObjectStore, FolderObjectStore, S3ObjectStore)`
把三条 OS 路径(open / reveal / copy)钉死在三个具体类上;新实现改走
`local_path(key)` / `uri(key)` 能力探测 —— 任意后端只要声明能力即可,
无需改 `_media_os_actions.py`。

替身 `UriOnlyStore` 是"第 4 种后端":既不是 Local/Folder 也不是 S3,
只 override `uri()`(模拟远端 `gs://` / `s3a://` 之类的新存储)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tgmonitor.core.dto import (
    CopyResult,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    RevealResult,
)
from tgmonitor.core.media_service._media_os_actions import (
    copy_media_path,
    reveal_in_folder,
)
from tgmonitor.core.objectstore.base import ObjectMeta, ObjectStore

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.storage.repository import StorageRepository


class UriOnlyStore(ObjectStore):
    """第 4 种后端替身:无本地路径,只给 `gs://` URI。

    不是 Local/Folder/S3 任一具体类 —— 老 isinstance 实现会对它返回
    "不支持的对象存储后端";能力探测实现则能通过 `uri()` 正常工作。
    """

    backend_name = "gcs-fake"

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def put(self, key: str, data: bytes, meta: ObjectMeta | None = None) -> str:
        self._data[key] = data
        return key

    async def get(self, key: str) -> bytes:
        return self._data[key]

    async def exists(self, key: str) -> bool:
        return key in self._data

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def stat(self, key: str) -> ObjectMeta | None:
        return ObjectMeta(size=len(self._data[key])) if key in self._data else None

    def uri(self, key: str) -> str | None:
        # 唯一声明的能力:远端 URI(老实现会把它判为不支持)
        return f"gs://fake-bucket/{key}"


def _done_media(key: str) -> MediaDTO:
    return MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name="photo.jpg",
        file_size=0,
        object_key=key,
        object_backend="gcs-fake",
        download_status=MediaDownloadStatus.DONE,
    )


def _msg() -> MessageDTO:
    return MessageDTO(
        id=0,
        channel_id=100,
        telegram_msg_id=1,
        text="hi",
        media=[_done_media("media/p.jpg")],
    )


def test_objectstore_capability_defaults_are_none() -> None:
    """未 override 的后端默认无两种能力 —— 现有后端不破坏。"""

    class Bare(ObjectStore):
        backend_name = "bare"

        async def connect(self) -> None:
            pass

        async def close(self) -> None:
            pass

        async def put(self, key, data, meta=None) -> str:
            return key

        async def get(self, key) -> bytes:
            return b""

        async def exists(self, key) -> bool:
            return False

        async def delete(self, key) -> None:
            pass

        async def stat(self, key) -> ObjectMeta | None:
            return None

    bare = Bare()
    assert bare.local_path("media/p.jpg") is None
    assert bare.uri("media/p.jpg") is None


@pytest.mark.asyncio
async def test_new_backend_uri_copy_works_without_core_change(
    app: AppService,
    storage: StorageRepository,
) -> None:
    """第 4 种后端只需 override uri() → copy_media_path 直接可用。"""
    store = UriOnlyStore()
    app.objects = store  # type: ignore[assignment]
    await storage.save_message(_msg())

    result = await copy_media_path(app.media, 100, 1, 0)

    assert isinstance(result, CopyResult)
    assert result.success is True
    assert result.copied_value == "gs://fake-bucket/media/p.jpg"


@pytest.mark.asyncio
async def test_new_backend_reveal_reports_no_local_path(
    app: AppService,
    storage: StorageRepository,
) -> None:
    """第 4 种远端后端 reveal → 有 uri 无 local_path,给"无本地路径"提示而非"不支持"。"""
    app.objects = UriOnlyStore()  # type: ignore[assignment]
    await storage.save_message(_msg())

    result = await reveal_in_folder(app.media, 100, 1, 0)

    assert isinstance(result, RevealResult)
    assert result.success is False
    assert result.error is not None
    assert "无本地路径" in result.error
    assert "不支持的对象存储后端" not in result.error
