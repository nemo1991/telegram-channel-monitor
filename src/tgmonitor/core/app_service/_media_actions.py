"""AppService media 域操作 — 2026-10-12 plan-batch3 目标 2 step 2.2。

从 `AppService` 抽提 14 个 media 相关 method:
- 3 个真实逻辑:`retry_media` / `reveal_in_folder` / `_spawn_reveal`
- 1 个走 monitor 的:`delete_message`(UI 删单条消息,assert + monitor.delete_message)
- 10 个 `self._media.<x>` thin 转发(`list_media` / `delete_media` /
  `preview_delete_by_channel` / `delete_by_channel` / `load_thumbnail_bytes` /
  `load_media_bytes` / `open_media` / `open_media_with_result` /
  `copy_media_path` / `reconcile_orphans`)

设计原则(同 `_backend_sync`):
- plain module-level function(非 class)
- 第一参数 `app: AppService`(走 `app._media` / `app._storage` /
  `app._objects` / `app.monitor` / `app.bus` mutable state)
- 公共方法签名 1:1 保留 — UI 现有调用面零变化

为什么 retry_media / reveal_in_folder 留 facade 落地实现不放 media_service:
- 2026-08-29 v1.5.0 PR #A2 测试 `app.downloader = stub` 不生效 — 子 service
  仍持旧 downloader。retry_media 走 `app.downloader.download_one(...)` 必须
  facade 落地。`reveal_in_folder` 同理 — 测试 `app._spawn_reveal = staticmethod(fake)`
  必须 facade 生效。
- 2026-10-12 拆 facade 不改这条契约,继续 facade 落地。
"""

from __future__ import annotations

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
from tgmonitor.core.events import MediaDownloaded, MediaRetried

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService

log = logging.getLogger(__name__)


# ---------- 10 个 thin 转发(走 self._media) ----------


async def list_media(
    app: AppService,
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
    """转发 MediaService.list_media。"""
    return await app._media.list_media(
        channel_id=channel_id,
        status=status,
        media_type=media_type,
        search=search,
        limit=limit,
        offset=offset,
        sort=sort,
        sort_dir=sort_dir,
    )


async def delete_media(
    app: AppService, channel_id: int, telegram_msg_id: int, media_idx: int
) -> None:
    """转发 MediaService.delete_media。"""
    await app._media.delete_media(channel_id, telegram_msg_id, media_idx)


async def preview_delete_by_channel(app: AppService, channel_id: int) -> DeleteChannelPreview:
    """转发 MediaService.preview_delete_by_channel。"""
    return await app._media.preview_delete_by_channel(channel_id)


async def delete_by_channel(app: AppService, channel_id: int) -> int:
    """转发 MediaService.delete_by_channel。"""
    return await app._media.delete_by_channel(channel_id)


async def load_thumbnail_bytes(
    app: AppService,
    media: MediaDTO,
    *,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> bytes | None:
    """2026-09-29:转发 MediaService.load_thumbnail_bytes — 多带 msg_pk 三元组
    (channel_id + msg_id + media_idx) 定位独立 thumbnails 表的 thumb 行。
    """
    return await app._media.load_thumbnail_bytes(
        media,
        channel_id=channel_id,
        telegram_msg_id=telegram_msg_id,
        media_idx=media_idx,
    )


async def load_media_bytes(app: AppService, media: MediaDTO) -> bytes | None:
    """2026-08-31 v1.5.0 PR #A8:Lightbox 全屏预览转发 — 读原图 bytes。"""
    return await app._media.load_media_bytes(media)


async def open_media(
    app: AppService,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> bool:
    """转发 MediaService.open_media(向后兼容 wrapper)。"""
    return await app._media.open_media(channel_id, telegram_msg_id, media_idx)


async def open_media_with_result(
    app: AppService,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> OpenMediaResult:
    """转发 MediaService.open_media_with_result。"""
    return await app._media.open_media_with_result(channel_id, telegram_msg_id, media_idx)


async def copy_media_path(
    app: AppService,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> CopyResult:
    """转发 MediaService.copy_media_path。"""
    return await app._media.copy_media_path(channel_id, telegram_msg_id, media_idx)


async def reconcile_orphans(app: AppService, *, dry_run: bool = True):
    """转发 MediaService.reconcile_orphans。"""
    return await app._media.reconcile_orphans(dry_run=dry_run)


# ---------- 1 个走 monitor 的(delete_message) ----------


async def delete_message(app: AppService, channel_id: int, telegram_msg_id: int) -> None:
    """2026-09-08 v1.7.0:单条消息删除 — 转发 MonitorService.delete_message。

    MessageDetail 顶部「删除」按钮 + 右键单条菜单调用入口。
    """
    # `monitor` 在 AppService 启动后才赋值,单 delete 入口属 UI 触发
    # (已登录),此契约下必非 None;仍走 assert 锁死。
    assert app.monitor is not None
    await app.monitor.delete_message(channel_id, telegram_msg_id)


# ---------- 3 个真实逻辑(retry / reveal / spawn_reveal) ----------


async def retry_media(
    app: AppService,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> None:
    """重下 FAILED media:`objects.delete(old_key)` + download_one(force=True)。

    2026-08-29 v1.5.0 PR #A2:**留 facade 实现**(原搬至 MediaService 后
    测试 `app.downloader = stub` 不生效 — 子 service 仍持旧 downloader)。
    现在 facade 自己持 `self.downloader` 属性,monkeypatch 后立即生效。

    非 FAILED 状态直接返回(不报错,UI 通常 disable Retry 按钮,这里兜底)。
    成功后回写 storage + 发 MediaDownloaded(LIVE view 据此刷新状态)。
    """
    import dataclasses

    msg = await app.storage.get_message(channel_id, telegram_msg_id)
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
    new_msg = dataclasses.replace(msg, media=new_media)
    await app.storage.update_message(new_msg)
    # 清旧 bytes — 让 download_one(force=True) 一定走真下载路径
    if old_object_key and app.objects is not None:
        try:
            await app.objects.delete(old_object_key)
        except Exception:  # noqa: BLE001
            log.warning(
                "retry pre-clean bytes %s failed",
                old_object_key,
                exc_info=True,
            )
    # 先发 MediaRetried,UI 立刻把状态切到 PENDING(避免用户重复点 Retry)
    await app.bus.publish(
        MediaRetried(
            channel_id=channel_id,
            telegram_msg_id=telegram_msg_id,
            media_idx=media_idx,
        )
    )
    # 然后走同步下载路径(retry 走 facade 直调,不走 worker queue —
    # 2026-08-24 D4:不增加 force flag 进 queue,避免协议变更)
    if app.downloader is not None:
        try:
            updated = await app.downloader.download_one(
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
        final_msg = dataclasses.replace(new_msg, media=final_media)
        await app.storage.update_message(final_msg)
        await app.bus.publish(
            MediaDownloaded(
                channel_id=channel_id,
                telegram_msg_id=telegram_msg_id,
                media=updated,
            )
        )


async def reveal_in_folder(
    app: AppService,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> RevealResult:
    """2026-08-27 v1.4.0 PR #16:在文件管理器中高亮 media 文件(macOS
    Finder / Windows Explorer / Linux xdg-open 父目录)。

    2026-08-29 v1.5.0 PR #A2:**留 facade 实现**(同 retry_media 原因 —
    测设 `app._spawn_reveal = staticmethod(fake)` 必须 facade 上生效)。

    仅 Local / Folder 后端有效:S3 无本地文件,S3 路径应走 `copy_media_path`
    拿 URI。失败原因以 `RevealResult.error` 返回,UI 据此弹 QMessageBox。
    """
    import asyncio as _asyncio
    import sys as _sys

    from tgmonitor.core.objectstore.folder_store import FolderObjectStore
    from tgmonitor.core.objectstore.local_store import LocalObjectStore
    from tgmonitor.core.objectstore.s3_store import S3ObjectStore

    msg = await app.storage.get_message(channel_id, telegram_msg_id)
    if msg is None or media_idx >= len(msg.media):
        return RevealResult(False, "消息或媒体不存在")
    med = msg.media[media_idx]
    if not med.object_key or med.download_status != MediaDownloadStatus.DONE:
        return RevealResult(False, "媒体未下载完成")
    if isinstance(app.objects, S3ObjectStore):
        return RevealResult(
            False,
            "S3 后端无本地路径:请使用「Copy 路径」拿到 s3:// URI",
        )
    if not isinstance(app.objects, (LocalObjectStore, FolderObjectStore)):
        return RevealResult(
            False,
            f"不支持的对象存储后端: {type(app.objects).__name__}",
        )

    try:
        abs_path = app.objects._path(med.object_key)  # noqa: SLF001
        if not abs_path.exists():
            return RevealResult(False, f"文件不存在: {abs_path}")
        # OS-specific 唤起文件管理器(macOS `open -R` 高亮 / Windows
        # `explorer /select,` / Linux `xdg-open <parent_dir>`)
        # 2026-10-12 plan-batch3 step 2.2:走 `app._spawn_reveal`(facade
        # `__init__` 末尾 `self._spawn_reveal = staticmethod(_spawn_reveal)`
        # 默认绑定到本 module 函数)。测试 `app._spawn_reveal =
        # staticmethod(fake)` monkeypatch 模式继续生效 — 与原 facade 行为
        # 等价,只换了默认函数位置。
        await _asyncio.to_thread(
            app._spawn_reveal,
            abs_path,
            _sys.platform,
        )
        return RevealResult(True)
    except Exception as exc:  # noqa: BLE001 — 收口,UI 不应见堆栈
        return RevealResult(False, f"{type(exc).__name__}: {exc}")


def _spawn_reveal(abs_path, platform: str) -> None:
    """2026-08-27 v1.4.0 PR #16:同步 spawn 子进程唤起 OS 文件管理器。

    - macOS:`open -R <abs_path>`(在 Finder 高亮该文件)
    - Windows:`explorer /select,<abs_path>`(在 Explorer 高亮)
    - Linux / 其它:`xdg-open <abs_path.parent>`(打开父目录,Linux 无
      标准「高亮」API,降级开父目录)

    2026-10-12 plan-batch3 step 2.2:从 `AppService._spawn_reveal` 静态方法
    抽出到本 module 顶层 plain function。facade `__init__` 末尾
    `self._spawn_reveal = staticmethod(_spawn_reveal)` 默认绑定到此,
    测试 `app._spawn_reveal = staticmethod(fake)` monkeypatch 模式
    继续生效(`reveal_in_folder` 通过 `app._spawn_reveal` 间接取)。
    """
    import subprocess

    if platform == "darwin":
        subprocess.Popen(["open", "-R", str(abs_path)])
    elif platform == "win32":
        subprocess.Popen(["explorer", f"/select,{abs_path}"])
    else:
        subprocess.Popen(["xdg-open", str(abs_path.parent)])
