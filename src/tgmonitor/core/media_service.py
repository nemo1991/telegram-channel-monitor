"""MediaService — Media Manager 域 façade(从 `AppService` 拆出)。

2026-08-29 v1.5.0 PR #A2:把 AppService 中所有「media 操作」抽到这里,
AppService 退化为组合根 + 转发。包含:
  - 媒体列表 / 计数:`list_media`
  - 媒体 CRUD:`delete_media` / `delete_by_channel` / `preview_delete_by_channel` /
    `retry_media`
  - 媒体打开:`load_thumbnail_bytes` / `open_media_with_result` / `open_media` /
    `reveal_in_folder` / `copy_media_path` / `_stage_s3_to_tmp`
  - 存储健康:`reconcile_orphans`
  - 内部 helper:`_spawn_reveal`(同步 spawn 子进程唤起 OS 文件管理器)

设计:
  - 持 `bus + storage + objects + downloader`;订阅 / 鉴权 / 同步归其它 service
  - `_stage_s3_to_tmp` 复用,`open_media_with_result` / `reveal_in_folder` /
    `copy_media_path` 都按 `isinstance(self._objects, ...)` 分支(本地 / S3)
  - 公共方法签名 1:1 转发,UI 现有调用面不动
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import mimetypes
import os
import secrets
import sys
from pathlib import Path
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
    MediaDeleted,
    MediaDownloaded,
    MediaReconcileFinished,
    MediaRetried,
)
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository

if TYPE_CHECKING:
    from tgmonitor.core.monitor.service import MediaDownloader

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

    # ---------- 列表 ----------

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
        """列出已下载 / 失败 / 下载中媒体(2026-08-25 PR #3 下沉)— 转发 storage。

        filter 全部下沉到 `StorageRepository.list_media` 后端(InMemory /
        Jsonl 顺序扫 + slice,Postgres SQL JOIN,Mongo aggregate),UI 不再
        触碰应用层 flatten。

        2026-08-25 v1.3.0 PR #6:返 `(rows, total)` — `total` 来自
        `storage.count_media` 走同一组 filter 但不带 sort/limit/offset,
        供 Media Manager 状态栏显示「Page N/M · total」。
        """
        channel_ids = [channel_id] if channel_id is not None else None
        rows = await self._storage.list_media(
            channel_ids=channel_ids,
            status=status,
            media_type=media_type,
            search=search,
            limit=limit,
            offset=offset,
            sort=sort,
            sort_dir=sort_dir,
        )
        total = await self._storage.count_media(
            channel_ids=channel_ids,
            status=status,
            media_type=media_type,
            search=search,
        )
        return rows, total

    # ---------- CRUD ----------

    async def delete_media(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        """摘 media from message + refcount=0 时清 bytes + 发 MediaDeleted。

        与 `MonitorService.delete_message` 的 bytes 清理语义一致:跨消息去重
        场景下,另一 message 仍引用同 `object_key` 时只摘当前 message 的 media,
        不动 bytes;无引用时 `objects.delete(key)` 释放磁盘。
        """
        msg = await self._storage.get_message(channel_id, telegram_msg_id)
        if msg is None or media_idx >= len(msg.media):
            return
        med = msg.media[media_idx]
        object_key = med.object_key
        new_media = msg.media[:media_idx] + msg.media[media_idx + 1 :]
        new_msg = dataclasses.replace(msg, media=new_media)
        await self._storage.update_message(new_msg)
        if object_key:
            try:
                n = await self._storage.count_media_by_object_key(object_key)
            except Exception:  # noqa: BLE001
                log.exception("count media by key failed: %s", object_key)
                n = 0
            if n == 0 and self._objects is not None:
                try:
                    await self._objects.delete(object_key)
                except Exception:  # noqa: BLE001
                    log.warning(
                        "delete bytes %s failed (already gone?)",
                        object_key,
                        exc_info=True,
                    )
        await self._bus.publish(
            MediaDeleted(
                channel_id=channel_id,
                telegram_msg_id=telegram_msg_id,
                media_idx=media_idx,
            )
        )

    async def preview_delete_by_channel(
        self,
        channel_id: int,
    ) -> DeleteChannelPreview:
        """2026-08-25 v1.3.0 PR #8:Clear Channel dry-run 预览。

        **严格只读** — 不调任何 `delete_*` API。返回 `DeleteChannelPreview`:
        - `message_count` 走 `storage.count_messages(channel_id)`
        - `media_count` 走新 abstract `storage.count_media_by_channel`
        - `potential_orphan_bytes`:模拟 `delete_by_channel` 的 refcount 清理
          路径,只累加「refcount=1 且 file_size 非 None」的 DONE media 的字节
          (跨频道共享的不计,跟实际 `objects.delete` 触发条件一致)

        空 channel / 无 media 时返全 0 dataclass,不抛。
        """
        msg_count = await self._storage.count_messages(channel_id)
        media_count = await self._storage.count_media_by_channel(channel_id)
        if msg_count == 0 and media_count == 0:
            return DeleteChannelPreview(
                channel_id=channel_id,
                message_count=0,
                media_count=0,
                potential_orphan_bytes=0,
            )

        # 只取 DONE 的 media,跟 delete_by_channel 实际清理的对象一致
        done_rows = await self._storage.list_media(
            channel_ids=[channel_id],
            status=MediaDownloadStatus.DONE,
            limit=1_000_000,
            offset=0,
        )
        orphan_bytes = 0
        seen: set[str] = set()
        for _msg, _idx, med in done_rows:
            if not med.object_key or med.object_key in seen:
                continue
            seen.add(med.object_key)
            try:
                n = await self._storage.count_media_by_object_key(med.object_key)
            except Exception:  # noqa: BLE001
                log.exception(
                    "preview count_media_by_object_key failed: %s",
                    med.object_key,
                )
                n = 0
            if n <= 1 and med.file_size:
                orphan_bytes += med.file_size
        return DeleteChannelPreview(
            channel_id=channel_id,
            message_count=msg_count,
            media_count=media_count,
            potential_orphan_bytes=orphan_bytes,
        )

    async def delete_by_channel(self, channel_id: int) -> int:
        """2026-08-25 PR #4:批量删某频道所有 message + 顺手清孤儿 bytes。

        与 `MonitorService.delete_message` 的 bytes 清理语义一致:对每条
        待删 message 的每个 `media.object_key` 做 refcount 检查,=0 时
        调 `objects.delete(key)`。

        跨频道行为:不动其它 channel 的 message / media — 用户典型诉求
        「这个频道我不想留媒体,一键清空」,只清目标频道。

        退出语义:中途 storage.delete_message 抛错 → 已删除的不回滚,
        异常上抛让调用方知道部分成功(2026-08-25:用户确认「不要回滚,
        上抛提示」即可,后续按需加 dry-run preview)。
        """
        msgs = await self._storage.list_messages([channel_id], limit=None)
        deleted = 0
        for msg in msgs:
            # 1) 先记下该 message 的所有 object_key(用于后续 refcount)
            keys: list[str] = [
                med.object_key
                for med in msg.media
                if med.object_key and med.download_status == MediaDownloadStatus.DONE
            ]
            try:
                await self._storage.delete_message(channel_id, msg.telegram_msg_id)
            except Exception:  # noqa: BLE001
                log.exception(
                    "delete_by_channel partial failure: channel=%s msg=%s",
                    channel_id,
                    msg.telegram_msg_id,
                )
                continue
            deleted += 1
            # 2) 删 message 后,逐 key 检查 refcount;=0 则清 bytes
            for key in keys:
                try:
                    n = await self._storage.count_media_by_object_key(key)
                except Exception:  # noqa: BLE001
                    log.exception("count media by key failed: %s", key)
                    continue
                if n == 0 and self._objects is not None:
                    try:
                        await self._objects.delete(key)
                    except Exception:  # noqa: BLE001
                        log.warning(
                            "delete_by_channel bytes %s failed",
                            key,
                            exc_info=True,
                        )
        log.info(
            "delete_by_channel: channel=%s deleted=%d",
            channel_id,
            deleted,
        )
        return deleted

    async def retry_media(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        """重下 FAILED media:`objects.delete(old_key)` + download_one(force=True)。

        非 FAILED 状态直接返回(不报错,UI 通常 disable Retry 按钮,这里兜底)。
        成功后回写 storage + 发 MediaDownloaded(LIVE view 据此刷新状态)。
        """
        msg = await self._storage.get_message(channel_id, telegram_msg_id)
        if msg is None or media_idx >= len(msg.media):
            return
        med = msg.media[media_idx]
        if med.download_status != MediaDownloadStatus.FAILED:
            return
        old_object_key = med.object_key
        new_med = dataclasses.replace(
            med,
            object_key=None,
            object_backend=None,
            download_status=MediaDownloadStatus.PENDING,
            download_error=None,
        )
        new_media = list(msg.media)
        new_media[media_idx] = new_med
        new_msg = dataclasses.replace(msg, media=list(new_media))
        await self._storage.update_message(new_msg)
        # 清旧 bytes — 让 download_one(force=True) 一定走真下载路径
        if old_object_key and self._objects is not None:
            try:
                await self._objects.delete(old_object_key)
            except Exception:  # noqa: BLE001
                log.warning(
                    "retry pre-clean bytes %s failed",
                    old_object_key,
                    exc_info=True,
                )
        # 先发 MediaRetried,UI 立刻把状态切到 PENDING(避免用户重复点 Retry)
        await self._bus.publish(
            MediaRetried(
                channel_id=channel_id,
                telegram_msg_id=telegram_msg_id,
                media_idx=media_idx,
            )
        )
        # 然后走同步下载路径(retry 走 MediaService 直调,不走 worker queue —
        # 2026-08-24 D4:不增加 force flag 进 queue,避免协议变更)
        if self._downloader is not None:
            try:
                updated = await self._downloader.download_one(
                    msg_pk=msg.id,
                    media=new_med,
                    force=True,
                )
            except Exception as e:  # noqa: BLE001
                log.exception("retry download failed: %s", e)
                updated = dataclasses.replace(
                    new_med,
                    download_status=MediaDownloadStatus.FAILED,
                    download_error=f"重试异常: {e}",
                )
            # 回写最终状态
            final_media = list(new_msg.media)
            final_media[media_idx] = updated
            final_msg = dataclasses.replace(new_msg, media=list(final_media))
            await self._storage.update_message(final_msg)
            await self._bus.publish(
                MediaDownloaded(
                    channel_id=channel_id,
                    telegram_msg_id=telegram_msg_id,
                    media=updated,
                )
            )

    # ---------- 打开 / 预览 ----------

    async def load_thumbnail_bytes(self, media: MediaDTO) -> bytes | None:
        """读 media 的缩略图 bytes — UI 渲染缩略图用(2026-08-25 PR #1)。

        优先 `thumb_key`(TG 端小缩略图,通常 90×90 JPEG);缺失则用
        `object_key` 原图(decoder 仍能 render)。仅 DONE + 有 objectstore 时
        才读;任何异常返 None 让 UI 保持 emoji 占位。

        设计取舍(2026-08-25 PR #1 E1):
        - 全量读 bytes 不流式 — 缩略图一般 ≤ 50KB,本地 FS / S3 都是单次
          GET;流式 (open_read iterator) 在这里收益小于代码复杂度。
        - 不调 LRU 缓存(进程内 UI 层做,service 层不持 Qt 状态)。
        - 用 `objects.open_read(key)` 而非 `get(key)` — 接口统一,Local /
          Folder 后端都用 BytesIO;失败时仍走 try/except 兜底。
        """
        if media.download_status != MediaDownloadStatus.DONE:
            return None
        if self._objects is None:
            return None
        backend = media.object_backend
        if not backend:
            return None
        # thumb 优先,缺则用原图
        key = media.thumb_key or media.object_key
        if not key:
            return None
        try:
            stream = await self._objects.open_read(key)
            try:
                data = stream.read()
            finally:
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
            return data
        except Exception:  # noqa: BLE001 — KeyError / S3 ClientError / 任何错
            log.warning(
                "load_thumbnail_bytes failed: backend=%s key=%s",
                backend,
                key,
                exc_info=True,
            )
            return None

    async def load_media_bytes(self, media: MediaDTO) -> bytes | None:
        """读 media **原图** bytes — Lightbox 全屏预览用(2026-08-31 v1.5.0 PR #A8)。

        与 `load_thumbnail_bytes` 区别:**不优先 thumb_key**,只读 `object_key`
        原图(thumb 90×90 太小,Lightbox 显示会糊)。仅 DONE + 有 objectstore 时
        才读;任何异常返 None。

        设计取舍:
        - 全量读 bytes(同 thumbnail)— 单图通常 ≤ 30MB,本地 FS / S3 都是
          单次 GET;流式读 QtQPixmap 不支持(只能 in-memory decode)
        - GIF / WebP / JPEG / PNG 都原样返回,Qt QPixmap.loadFromData 自动识别
        """
        if media.download_status != MediaDownloadStatus.DONE:
            return None
        if self._objects is None:
            return None
        backend = media.object_backend
        if not backend:
            return None
        key = media.object_key
        if not key:
            return None
        try:
            stream = await self._objects.open_read(key)
            try:
                data = stream.read()
            finally:
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
            return data
        except Exception:  # noqa: BLE001
            log.warning(
                "load_media_bytes failed: backend=%s key=%s",
                backend,
                key,
                exc_info=True,
            )
            return None

    async def open_media(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> bool:
        """系统默认程序打开 media 文件。True = 成功发起,False = 不可打开。

        2026-08-25 v1.3.0 PR #5:扩展到 S3 后端(走 `_stage_s3_to_tmp`)并暴露
        失败原因。返回 bool 是向后兼容 wrapper — 真实实现走 `open_media_with_result`,
        UI 失败时显示 reason。
        """
        return (await self.open_media_with_result(channel_id, telegram_msg_id, media_idx)).success

    async def open_media_with_result(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> OpenMediaResult:
        """打开 media + 返回结构化结果(2026-08-25 v1.3.0 PR #5)。

        Local / Folder:直接 `QDesktopServices.openUrl(QUrl.fromLocalFile(...))`。
        S3:把 ObjectStore bytes 写到 `QStandardPaths.TempLocation` 下的 tmp 文件
        再 openUrl;tmp 成功路径不主动 unlink(交给 OS 在 app exit / 重启时回收;
        Windows 上 OS 持有 handle 时 unlink 会失败,故意不冒此风险),失败路径
        显式 unlink 兜底。

        所有异常(对象存储连接断 / get 失败 / tmp 写失败 / openUrl 返 False)都
        收口到 OpenMediaResult,不让 UI 看到原始堆栈。
        """
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from tgmonitor.core.objectstore.folder_store import FolderObjectStore
        from tgmonitor.core.objectstore.local_store import LocalObjectStore
        from tgmonitor.core.objectstore.s3_store import S3ObjectStore

        msg = await self._storage.get_message(channel_id, telegram_msg_id)
        if msg is None or media_idx >= len(msg.media):
            return OpenMediaResult(False, "消息或媒体不存在")
        med = msg.media[media_idx]
        if not med.object_key or med.download_status != MediaDownloadStatus.DONE:
            return OpenMediaResult(False, "媒体未下载完成")

        try:
            if isinstance(self._objects, (LocalObjectStore, FolderObjectStore)):
                # 用 backend 自带的 _path 而非 self._objects._root / key —
                # FolderObjectStore 用 `media/<ab>/<cd>/<name>` 分片式相对路径,
                # 直接拼 root 会落到错的子目录。
                abs_path = self._objects._path(med.object_key)  # noqa: SLF001
                ok = bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(abs_path))))
                return (
                    OpenMediaResult(True)
                    if ok
                    else OpenMediaResult(False, "系统调用失败:请检查是否已关联默认应用")
                )
            if isinstance(self._objects, S3ObjectStore):
                tmp = await self._stage_s3_to_tmp(med)
                ok = bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(tmp))))
                if not ok:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    return OpenMediaResult(
                        False,
                        f"系统调用失败:无法打开临时文件 {tmp.name}",
                    )
                return OpenMediaResult(True)
            return OpenMediaResult(
                False,
                f"不支持的对象存储后端: {type(self._objects).__name__}",
            )
        except Exception as exc:  # noqa: BLE001 — 收口,UI 不应见堆栈
            return OpenMediaResult(False, f"{type(exc).__name__}: {exc}")

    async def reveal_in_folder(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> RevealResult:
        """2026-08-27 v1.4.0 PR #16:在文件管理器中高亮 media 文件(macOS
        Finder / Windows Explorer / Linux xdg-open 父目录)。

        仅 Local / Folder 后端有效:S3 无本地文件,S3 路径应走 `copy_media_path`
        拿 URI。失败原因以 `RevealResult.error` 返回,UI 据此弹 QMessageBox。
        """
        from tgmonitor.core.objectstore.folder_store import FolderObjectStore
        from tgmonitor.core.objectstore.local_store import LocalObjectStore
        from tgmonitor.core.objectstore.s3_store import S3ObjectStore

        msg = await self._storage.get_message(channel_id, telegram_msg_id)
        if msg is None or media_idx >= len(msg.media):
            return RevealResult(False, "消息或媒体不存在")
        med = msg.media[media_idx]
        if not med.object_key or med.download_status != MediaDownloadStatus.DONE:
            return RevealResult(False, "媒体未下载完成")
        if isinstance(self._objects, S3ObjectStore):
            return RevealResult(
                False,
                "S3 后端无本地路径:请使用「Copy 路径」拿到 s3:// URI",
            )
        if not isinstance(self._objects, (LocalObjectStore, FolderObjectStore)):
            return RevealResult(
                False,
                f"不支持的对象存储后端: {type(self._objects).__name__}",
            )

        try:
            abs_path = self._objects._path(med.object_key)  # noqa: SLF001
            if not abs_path.exists():
                return RevealResult(False, f"文件不存在: {abs_path}")
            # OS-specific 唤起文件管理器(macOS `open -R` 高亮 / Windows
            # `explorer /select,` / Linux `xdg-open <parent_dir>`)
            await asyncio.to_thread(
                self._spawn_reveal,
                abs_path,
                sys.platform,
            )
            return RevealResult(True)
        except Exception as exc:  # noqa: BLE001 — 收口,UI 不应见堆栈
            return RevealResult(False, f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _spawn_reveal(abs_path, platform: str) -> None:
        """2026-08-27 v1.4.0 PR #16:同步 spawn 子进程唤起 OS 文件管理器。

        - macOS:`open -R <abs_path>`(在 Finder 高亮该文件)
        - Windows:`explorer /select,<abs_path>`(在 Explorer 高亮)
        - Linux / 其它:`xdg-open <abs_path.parent>`(打开父目录,Linux 无
          标准「高亮」API,降级开父目录)
        """
        import subprocess

        if platform == "darwin":
            subprocess.Popen(["open", "-R", str(abs_path)])
        elif platform == "win32":
            subprocess.Popen(["explorer", f"/select,{abs_path}"])
        else:
            subprocess.Popen(["xdg-open", str(abs_path.parent)])

    async def copy_media_path(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> CopyResult:
        """2026-08-27 v1.4.0 PR #16:把 media 路径 / URI 写入剪贴板。

        - Local / Folder:绝对路径(`<root>/<object_key>`)
        - S3:`s3://<bucket>/<object_key>`(URI 字符串,不是本地路径)
        - 其它后端:失败 + 不支持提示
        """
        from tgmonitor.core.objectstore.folder_store import FolderObjectStore
        from tgmonitor.core.objectstore.local_store import LocalObjectStore
        from tgmonitor.core.objectstore.s3_store import S3ObjectStore

        msg = await self._storage.get_message(channel_id, telegram_msg_id)
        if msg is None or media_idx >= len(msg.media):
            return CopyResult(False, error="消息或媒体不存在")
        med = msg.media[media_idx]
        if not med.object_key or med.download_status != MediaDownloadStatus.DONE:
            return CopyResult(False, error="媒体未下载完成")
        if isinstance(self._objects, (LocalObjectStore, FolderObjectStore)):
            try:
                abs_path = self._objects._path(med.object_key)  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001
                return CopyResult(
                    False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            return CopyResult(True, copied_value=str(abs_path))
        if isinstance(self._objects, S3ObjectStore):
            # S3ObjectStore 通常有 bucket / key 拼接;s3a URI 也可,这里用 s3://
            try:
                bucket = (
                    getattr(self._objects, "bucket_name", None)
                    or getattr(self._objects, "bucket", None)
                    or getattr(self._objects, "_bucket", None)
                )
            except Exception:  # noqa: BLE001
                bucket = None
            if not bucket:
                return CopyResult(
                    False,
                    error="S3 后端未暴露 bucket 字段",
                )
            uri = f"s3://{bucket}/{med.object_key}"
            return CopyResult(True, copied_value=uri)
        return CopyResult(
            False,
            error=f"不支持的对象存储后端: {type(self._objects).__name__}",
        )

    async def _stage_s3_to_tmp(self, med: MediaDTO) -> Path:
        """2026-08-25 v1.3.0 PR #5:把 S3 media bytes 写到本地 tmp 文件用于
        `QDesktopServices.openUrl`。

        - 扩展名推断优先级:`med.file_name` 后缀 > `med.mime_type` 查
          `mimetypes.guess_extension` > `.bin` fallback
        - tmp 目录:`QStandardPaths.TempLocation`(macOS 是 per-user tmp),
          不可写时回退 `~/.cache/tgmonitor`
        - 文件名:`tgmonitor-<secrets.token_hex(8)><suffix>` — `tgmonitor-`
          前缀留给未来 sweep 工具批量清理
        - 写文件用 `asyncio.to_thread(tmp_path.write_bytes, data)` 防卡 loop
        """
        # 1. suffix
        suffix = ""
        if med.file_name:
            suffix = os.path.splitext(med.file_name)[1]
        if not suffix and med.mime_type:
            guessed = mimetypes.guess_extension(med.mime_type, strict=False)
            if guessed:
                suffix = guessed
        if not suffix:
            suffix = ".bin"

        # 2. tmp 目录
        try:
            from PySide6.QtCore import QStandardPaths

            tmp_dir_str = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.TempLocation,
            )
        except Exception:  # noqa: BLE001 — PySide6 不可用兜底
            tmp_dir_str = ""
        tmp_dir = Path(tmp_dir_str) if tmp_dir_str else Path.home() / ".cache" / "tgmonitor"
        await asyncio.to_thread(tmp_dir.mkdir, parents=True, exist_ok=True)

        # 3. 写文件
        tmp_path = tmp_dir / f"tgmonitor-{secrets.token_hex(8)}{suffix}"
        assert self._objects is not None  # ensure_objects 已检查
        data = await self._objects.get(med.object_key or "")
        await asyncio.to_thread(tmp_path.write_bytes, data)
        return tmp_path

    # ---------- 健康 ----------

    async def reconcile_orphans(self, *, dry_run: bool = True) -> MediaReconcileFinished:
        """扫描 ObjectStore vs storage 媒体索引,孤儿 = ObjectStore 里有但 storage 没引用。

        dry_run=True(默认)只 log 不删;Media Manager 「Prune Orphans」按钮显式
        触发 dry_run=False 真删。S3 后端:
        - 未连接(iter_keys raise RuntimeError)→ 当作 scanned=0 兜底
        - raise NotImplementedError(理论上不再发生)→ 同上兜底
        """
        backend = self._objects.backend_name if self._objects else ""
        scanned_keys: set[str] = set()
        referenced_keys: set[str] = set()
        if self._objects is not None and hasattr(self._objects, "iter_keys"):
            try:
                async for k in self._objects.iter_keys(prefix="media/"):
                    scanned_keys.add(k)
            except (NotImplementedError, RuntimeError) as e:
                # 2026-08-25 PR #2:加 RuntimeError(S3 未连接会 raise "未连接")
                log.info(
                    "reconcile skipped: %s backend iter_keys unavailable: %s",
                    backend,
                    e,
                )
        chs = await self._storage.list_channels()
        if chs:
            msgs = await self._storage.list_messages(
                [c.id for c in chs],
                limit=100_000,
            )
            for m in msgs:
                for med in m.media:
                    if med.object_key and med.download_status == MediaDownloadStatus.DONE:
                        referenced_keys.add(med.object_key)
        orphans = scanned_keys - referenced_keys
        deleted = 0
        if not dry_run and self._objects is not None and orphans:
            for k in orphans:
                try:
                    await self._objects.delete(k)
                    deleted += 1
                except Exception:  # noqa: BLE001
                    log.warning("reconcile delete %s failed", k, exc_info=True)
        evt = MediaReconcileFinished(
            backend=backend,
            scanned=len(scanned_keys),
            referenced=len(referenced_keys),
            orphans=len(orphans),
            deleted=deleted,
            dry_run=dry_run,
        )
        log.info(
            "reconcile: backend=%s scanned=%d referenced=%d orphans=%d deleted=%d dry_run=%s",
            backend,
            evt.scanned,
            evt.referenced,
            evt.orphans,
            evt.deleted,
            dry_run,
        )
        await self._bus.publish(evt)
        return evt
