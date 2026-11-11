"""MediaService — Media Manager 域服务。

2026-08-29 v1.5.0 PR #A2 从 AppService 拆出;2026-11-11 v2.0.0 Phase 3
起为媒体域唯一实现(AppService 侧的 retry/reveal 重复实现与
`_spawn_reveal` monkeypatch 兼容层已删)。包含:
  - 媒体列表 / 计数:`list_media`
  - 媒体 CRUD:`delete_media` / `delete_by_channel` / `preview_delete_by_channel` /
    `retry_media`
  - 媒体打开:`load_thumbnail_bytes` / `load_media_bytes` /
    `open_media_with_result` / `open_media` / `reveal_in_folder` /
    `copy_media_path` / `_stage_s3_to_tmp`
  - 存储健康:`reconcile_orphans`

设计:
  - 持 `bus + storage + objects + downloader`;订阅 / 鉴权 / 同步归其它 service
  - `open_media_with_result` / `reveal_in_folder` / `copy_media_path` 按
    `isinstance(self._objects, ...)` 分支(本地 / S3)
  - 实现按职责下沉 `_media_crud` / `_media_bytes` / `_media_os_actions` /
    `_media_reconcile` 子 module,本类 thin delegate
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from tgmonitor.core.dto import (
    CopyResult,
    DeleteChannelPreview,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    OpenMediaResult,
    RevealResult,
    SortDir,
    SortKey,
)
from tgmonitor.core.events import (
    EventBus,
    MediaReconcileFinished,
)
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository

if TYPE_CHECKING:
    from pathlib import Path

    from tgmonitor.core.monitor.service import MediaDownloader

# 2026-10-15 plan-batch6-1.3:`load_thumbnail_bytes` / `load_media_bytes` /
# `_read_objectstore_bytes` 3 个 method 抽到 `_media_bytes` 子 module。
from . import _media_bytes as _bytes

# 2026-10-15 plan-batch6-1.2:`list_media` / `delete_media` /
# `preview_delete_by_channel` / `delete_by_channel` 4 个 method 抽到
# `_media_crud` 子 module;`ms.X` 引用走 module attr(同 _bridge 模式),
# 避免 `from ._media_crud import list_media` 绑死到子 module obj。
from . import _media_crud as _crud

# 2026-10-15 plan-batch6-1.4:`open_media` / `open_media_with_result` /
# `reveal_in_folder` / `_spawn_reveal` / `copy_media_path` /
# `_stage_s3_to_tmp` / `retry_media` 7 个 method 抽到 `_media_os_actions`
# 子 module。`_spawn_reveal` 是该 module 的 plain function,测试直接
# monkeypatch `tgmonitor.core.media_service._media_os_actions._spawn_reveal`。
from . import _media_os_actions as _os_actions

# 2026-10-15 plan-batch6-1.5:`reconcile_orphans` 抽到 `_media_reconcile` 子 module。
from . import _media_reconcile as _reconcile

# 2026-10-15 plan-batch6-1.1:Qt callback 注入(state + register_qt_callbacks /
# _reset_qt_callbacks_for_testing)抽到 `_qt_bridge` 子 module,内部用
# `from . import _qt_bridge as _bridge` 形式访问,避免
# `from ._qt_bridge import _module_open_url_cb` 绑死到子 module obj
# 后 monkeypatch 改 `tgmonitor.core.media_service._module_open_url_cb` 失效。
# 外部 monkeypatch 字符串路径见各 test 文件注释(改指 _qt_bridge)。
#
# 2026-10-15 plan-batch6-1.4:facade 不再直接用 `_bridge._module_open_url_cb`
# / `_module_tmp_root_cb` — 7 个 method 全下沉到 `_media_os_actions.py`
# 内部用,facade 仅 thin delegate 透传。

log = logging.getLogger(__name__)


class MediaService:
    """Media Manager 域 — 媒体查询 / CRUD / 打开 / 健康。"""

    def __init__(
        self,
        bus: EventBus,
        storage: StorageRepository,
        objects: ObjectStore | None,
        downloader: MediaDownloader | None = None,
    ) -> None:
        """4 个依赖 + 内部状态。`objects` 可为 None(Media Manager 仍可查列表,
        仅 delete / open 路径受限);`downloader` 仅 `retry_media` 用到。
        """
        self._bus = bus
        self._storage = storage
        self._objects = objects
        self._downloader = downloader
        # 2026-09-22 v1.8.x:reconcile_orphans 并发重入锁 — 启动期 _startup_reconcile
        # (2s 后跑 dry_run=True)与用户立刻点 "Prune Orphans"(dry_run=False)若
        # 同时进 iter_keys + delete 循环,前一个 sleep 醒来也进 delete,同一孤儿
        # 被两个协程各删一次(objects.delete 幂等但 log 脏 + EventBus 双发
        # MediaReconcileFinished,UI footer 计数闪烁)。锁住一个 in-flight,
        # 二次并发直接 no-op + log skip,避免 race。
        self._reconcile_lock = asyncio.Lock()

    # ---------- 列表 ----------
    # 2026-10-15 plan-batch6-1.2:`list_media` / `delete_media` /
    # `preview_delete_by_channel` / `delete_by_channel` 4 个 method 下沉到
    # `_media_crud` 子 module,facade 1 行 thin delegate。子 module 走
    # `ms.X` 引用,不再持副本;签名 1:1 保留,UI / `app.media.X` 调用面零变化。

    async def list_media(
        self,
        *,
        channel_id: int | None = None,
        status: MediaDownloadStatus | None = None,
        media_type: MediaType | None = None,
        search: str = "",
        limit: int = 1000,
        offset: int = 0,
        sort: SortKey = SortKey.DATE,
        sort_dir: SortDir = SortDir.DESC,
    ) -> tuple[list[tuple[MessageDTO, int, MediaDTO]], int]:
        """2026-08-25 v1.3.0 PR #3/PR #6 — 转发 storage.list_media + count_media。

        2026-10-15 plan-batch6-1.2:thin delegate → `core.media_service._media_crud`。
        """
        return await _crud.list_media(
            self,
            channel_id=channel_id,
            status=status,
            media_type=media_type,
            search=search,
            limit=limit,
            offset=offset,
            sort=sort,
            sort_dir=sort_dir,
        )

    # ---------- CRUD ----------

    async def delete_media(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        """摘 media + refcount=0 清 bytes + 发 MediaDeleted。

        2026-10-15 plan-batch6-1.2:thin delegate → `core.media_service._media_crud`。
        """
        await _crud.delete_media(self, channel_id, telegram_msg_id, media_idx)

    async def preview_delete_by_channel(
        self,
        channel_id: int,
    ) -> DeleteChannelPreview:
        """2026-08-25 v1.3.0 PR #8:Clear Channel dry-run 预览(只读)。

        2026-10-15 plan-batch6-1.2:thin delegate → `core.media_service._media_crud`。
        """
        return await _crud.preview_delete_by_channel(self, channel_id)

    async def delete_by_channel(self, channel_id: int) -> int:
        """2026-08-25 PR #4:批量删某频道所有 message + 顺手清孤儿 bytes。

        2026-10-15 plan-batch6-1.2:thin delegate → `core.media_service._media_crud`。
        """
        return await _crud.delete_by_channel(self, channel_id)

    async def retry_media(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        """重下 FAILED media — 2026-08-24 D4。

        2026-10-15 plan-batch6-1.4:thin delegate → `core.media_service._media_os_actions`。
        """
        await _os_actions.retry_media(self, channel_id, telegram_msg_id, media_idx)

    # ---------- 打开 / 预览 ----------
    # 2026-10-15 plan-batch6-1.3:`load_thumbnail_bytes` /
    # `load_media_bytes` / `_read_objectstore_bytes` 3 个 method 抽到
    # `_media_bytes` 子 module,facade 1 行 thin delegate。

    async def load_thumbnail_bytes(
        self,
        media: MediaDTO,
        *,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> bytes | None:
        """2026-08-25 PR #1:UI 渲染缩略图 — 走 thumb 表(2026-09-29)。

        2026-10-15 plan-batch6-1.3:thin delegate → `core.media_service._media_bytes`。
        """
        return await _bytes.load_thumbnail_bytes(
            self,
            media,
            channel_id=channel_id,
            telegram_msg_id=telegram_msg_id,
            media_idx=media_idx,
        )

    async def _read_objectstore_bytes(self, backend: str, key: str) -> bytes | None:
        """2026-10-15 plan-batch6-1.3:thin delegate → `core.media_service._media_bytes`。

        facade 保留该私有 helper 是给 `media_service` 内部少数 historical
        caller(暂未发现,留 facade 兼容后续扩展)。
        """
        return await _bytes._read_objectstore_bytes(self, backend, key)

    async def load_media_bytes(self, media: MediaDTO) -> bytes | None:
        """2026-08-31 v1.5.0 PR #A8:Lightbox 全屏预览原图。

        2026-10-15 plan-batch6-1.3:thin delegate → `core.media_service._media_bytes`。
        """
        return await _bytes.load_media_bytes(self, media)

    # ---------- 打开 / 预览 ----------
    # 2026-10-15 plan-batch6-1.4:`open_media` / `open_media_with_result` /
    # `reveal_in_folder` / `copy_media_path` / `_stage_s3_to_tmp` /
    # `retry_media` 抽到 `_media_os_actions` 子 module,facade 1 行 thin delegate。

    async def open_media(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> bool:
        """2026-08-25 v1.3.0 PR #5:系统默认程序打开 media — bool 兼容 wrapper。

        2026-10-15 plan-batch6-1.4:thin delegate → `core.media_service._media_os_actions`。
        """
        return await _os_actions.open_media(self, channel_id, telegram_msg_id, media_idx)

    async def open_media_with_result(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> OpenMediaResult:
        """2026-08-25 v1.3.0 PR #5:打开 media + 结构化结果(S3 / 本地)。

        2026-10-15 plan-batch6-1.4:thin delegate → `core.media_service._media_os_actions`。
        """
        return await _os_actions.open_media_with_result(
            self,
            channel_id,
            telegram_msg_id,
            media_idx,
        )

    async def reveal_in_folder(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> RevealResult:
        """2026-08-27 v1.4.0 PR #16:文件管理器高亮 media(macOS / Windows / Linux)。

        2026-10-15 plan-batch6-1.4:thin delegate → `core.media_service._media_os_actions`。
        """
        return await _os_actions.reveal_in_folder(self, channel_id, telegram_msg_id, media_idx)

    async def copy_media_path(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> CopyResult:
        """2026-08-27 v1.4.0 PR #16:把 media 路径 / URI 写入剪贴板。

        2026-10-15 plan-batch6-1.4:thin delegate → `core.media_service._media_os_actions`。
        """
        return await _os_actions.copy_media_path(
            self,
            channel_id,
            telegram_msg_id,
            media_idx,
        )

    async def _stage_s3_to_tmp(self, med: MediaDTO) -> Path:
        """2026-08-25 v1.3.0 PR #5:S3 bytes 写到本地 tmp(私有,open_url 用)。

        2026-10-15 plan-batch6-1.4:thin delegate → `core.media_service._media_os_actions`。
        """
        return await _os_actions._stage_s3_to_tmp(self, med)

    # ---------- 健康 ----------

    # ---------- 健康 ----------
    # 2026-10-15 plan-batch6-1.5:`reconcile_orphans` 抽到 `_media_reconcile` 子 module,
    # facade 1 行 thin delegate;mutex `ms._reconcile_lock` 透传。

    async def reconcile_orphans(
        self,
        *,
        dry_run: bool = True,
    ) -> MediaReconcileFinished:
        """扫描 ObjectStore vs storage 媒体索引,孤儿 = ObjectStore 里有但 storage 没引用。

        2026-10-15 plan-batch6-1.5:thin delegate → `core.media_service._media_reconcile`。
        """
        return await _reconcile.reconcile_orphans(self, dry_run=dry_run)
