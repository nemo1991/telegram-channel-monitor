# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""MediaMixin — Media Manager 事件转发 + UI 入口方法集合 — 2026-10-20 plan-batch9-ε。

`MonitorViewModel` 拆 5 mixin + BusMixin 第五步(2026-10-20
plan-batch9-ε):media 域 = 5 handler + 10 method + 10 signal —
最大块,触发 plan-batch6 media_service 875→329 / plan-batch9
message_view 1028→318 的同一条拆分线。

- 5 handler:`_on_media_downloaded` / `_on_media_download_progress` /
  `_on_media_retried` / `_on_media_deleted` /
  `_on_media_reconcile_done`
- 10 method:`load_media_list` / `delete_media` / `delete_media_batch` /
  `retry_media` / `open_media` / `reconcile_orphans` /
  `preview_delete_by_channel` / `delete_by_channel` / `load_thumbnail` /
  `load_media_bytes`(async,Lightbox 专用)
- 10 signal:Media Manager 全套事件转发 + UI 反馈
  - `media_downloaded` / `media_download_progress` — 下载事件
  - `media_list_loaded` / `media_retried` / `media_deleted` /
    `media_reconcile_done` — Media Manager 转发
  - `thumbnail_loaded` / `open_media_failed` / `delete_preview_ready` /
    `channel_cleared` — UI 反馈

跨 mixin access:
- `_on_media_downloaded` 调 `self.app.bus.publish(NotificationRequested(...))` —
  `app` 是 facade 实例字段(非 mixin 共享);`NotificationRequested` 是 core
  event 类,跨 mixin 构造实例无问题
- `load_thumbnail` 内部用 `from tgmonitor.core.dto import MediaDTO`(函数级
  局部 import)— 保持 DTO 与 mixin 解耦
- `load_media_bytes` async,直接 `await self.app.load_media_bytes(media)`
  — facade 实例方法

字段保留:Media 域不持有 facade 私有字段;`_export_task` 是 ExportMixin
业务(本 mixin 不持)。

单点 init 收敛:`known_channels` / `_export_task` / `_message_count`
仍 facade `__init__` 兜底(plan §8 风险 #2 惯例)。

MRO:`MonitorViewModel(ChannelsMixin, SystemMixin, MessagesMixin,
MediaMixin, BusMixin, QObject)` — MediaMixin 在 MessagesMixin 之后,
BusMixin 末(QObject 兜底 super().__init__())。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal

from tgmonitor.core.dto import (
    DeleteChannelPreview,
    MediaDownloadStatus,
    MediaType,
    SortDir,
    SortKey,
)
from tgmonitor.core.events import (
    MediaDeleted,
    MediaDownloaded,
    MediaDownloadProgress,
    MediaReconcileFinished,
    MediaRetried,
    NotificationRequested,
)
from tgmonitor.ui.async_bridge import run_coro

if TYPE_CHECKING:
    from PySide6.QtCore import QObject

    _Base = QObject
else:
    _Base = object

log = logging.getLogger(__name__)


class MediaMixin(_Base):
    """Media Manager 事件转发 + UI 入口 — 2026-10-20 plan-batch9-ε。

    公开 API 1:1 保留原 `MonitorViewModel` media 域:
    - 10 method:`load_media_list` / `delete_media` / `delete_media_batch` /
      `retry_media` / `open_media` / `reconcile_orphans` /
      `preview_delete_by_channel` / `delete_by_channel` / `load_thumbnail` /
      `load_media_bytes`
    - 10 signal:见 module docstring
    - 5 EventBus handler:`MediaDownloaded` / `MediaDownloadProgress` /
      `MediaRetried` / `MediaDeleted` / `MediaReconcileFinished`
    """

    # Media Manager(2026-08-24 新增)+ 后续 PR
    media_downloaded = Signal(object)  # MediaDownloaded(下载结束,成功或失败)
    # 2026-09-01 v1.5.1 PR #B3:下载进度节流转发。`Signal(object)` 承载
    # `MediaDownloadProgress` 事件本身;UI 在
    # media_manager_widget.on_download_progress 接到后查 row + 更新
    # 「已下载 X / Y (Z%)」文字。VM 不做节流(节流在 tdlib_client 端
    # 0.5s/次已完成;UI 端 Qt 内置 coalesce 也够用)。
    media_download_progress = Signal(object)
    media_list_loaded = Signal(object)  # list_media 返回值(list of (msg, idx, media))
    media_retried = Signal(object)  # MediaRetried 转发
    media_deleted = Signal(object)  # MediaDeleted 转发
    media_reconcile_done = Signal(object)  # MediaReconcileFinished 转发
    # 按频道批量删除完成(2026-08-25 PR #4)— payload (channel_id, deleted_count)
    channel_cleared = Signal(int, int)
    # 缩略图加载完成(2026-08-25 PR #1)— payload (channel_id, telegram_msg_id,
    # media_idx, bytes)。2026-10-15 plan-batch6-2:VM 退回纯 EventBus+signal
    # 适配器,QPixmap 渲染下沉到 widget 端(media_manager_widget._on_thumbnail_loaded
    # 自行调 `render_pixmap(bytes)`);信号 payload 改 `bytes` 后,Qt metatype
    # `QByteArray` 跨线程安全,VM 不再越界做 Qt 像素渲染(plan-batch3 误记
    # 「已修」,实测架构层 leak 没动)。
    thumbnail_loaded = Signal(int, int, int, bytes)
    # 打开媒体失败(2026-08-25 v1.3.0 PR #5)— payload
    # (channel_id, telegram_msg_id, media_idx, reason)
    open_media_failed = Signal(int, int, int, str)
    # Clear Channel 预览就绪(2026-08-25 v1.3.0 PR #8)— payload
    # DeleteChannelPreview(只读)。MainWindow 接到后弹
    # `ClearChannelPreviewDialog` 二次确认,确认后才走 `vm.delete_by_channel`。
    delete_preview_ready = Signal(object)

    _BUS_HANDLERS = {
        MediaDownloaded: "_on_media_downloaded",
        MediaDownloadProgress: "_on_media_download_progress",
        MediaRetried: "_on_media_retried",
        MediaDeleted: "_on_media_deleted",
        MediaReconcileFinished: "_on_media_reconcile_done",
    }

    # ---- 5 EventBus handler ----

    async def _on_media_downloaded(self, e: object) -> None:
        if not isinstance(e, MediaDownloaded):
            return
        self.media_downloaded.emit(e)
        # 2026-08-30 v1.5.0 PR #A4:成功下载 → 系统通知(走 TrayIcon /
        # status bar fallback)。失败不发(避免失败频通知扰人)。
        # 2026-08-30 PR #A4 简化:不实现 per-channel token bucket(plan 列了
        # 但短期内高频下载场景少;先发成功通知,后续 v1.5.1 加去抖)。
        if e.media and e.media.download_status == MediaDownloadStatus.DONE:
            await self.app.bus.publish(
                NotificationRequested(
                    level="info",
                    title="下载完成",
                    body=e.media.object_key or "(无文件名)",
                    click_action="show_main",
                )
            )

    async def _on_media_download_progress(self, e: object) -> None:
        """2026-09-01 v1.5.1 PR #B3:`MediaDownloadProgress` 事件 → emit Qt signal。

        UI 端 media_manager_widget.on_download_progress 接到后查 row +
        更新「已下载 X / Y (Z%)」文字。节流已由 TDLib 客户端 0.5s/次
        完成,这里直接透传。
        """
        if not isinstance(e, MediaDownloadProgress):
            return
        self.media_download_progress.emit(e)

    async def _on_media_retried(self, e: object) -> None:
        if not isinstance(e, MediaRetried):
            return
        self.media_retried.emit(e)

    async def _on_media_deleted(self, e: object) -> None:
        if not isinstance(e, MediaDeleted):
            return
        self.media_deleted.emit(e)

    async def _on_media_reconcile_done(self, e: object) -> None:
        if not isinstance(e, MediaReconcileFinished):
            return
        self.media_reconcile_done.emit(e)

    # ---- 10 public method ----

    def load_media_list(
        self,
        *,
        channel_id: int | None = None,
        status: MediaDownloadStatus | None = None,
        media_type: MediaType | None = None,
        search: str = "",
        limit: int = 1000,
        offset: int = 0,
        sort: SortKey | None = None,
        sort_dir: SortDir | None = None,
    ) -> None:
        """后台 fire app.list_media → emit media_list_loaded(rows, total)。

        2026-08-25 v1.3.0 PR #6 新增 sort / sort_dir / offset kwargs;不传
        走默认(DATE DESC,offset=0),向后兼容旧调用方。
        """

        async def _go() -> None:
            # 默认 None 透传(None 视作 "all",不过滤)
            st = status if isinstance(status, MediaDownloadStatus) else None
            mt = media_type if isinstance(media_type, MediaType) else None
            sk = sort if isinstance(sort, SortKey) else SortKey.DATE
            sd = sort_dir if isinstance(sort_dir, SortDir) else SortDir.DESC
            rows, total = await self.app.list_media(
                channel_id=channel_id,
                status=st,
                media_type=mt,
                search=search,
                limit=limit,
                offset=offset,
                sort=sk,
                sort_dir=sd,
            )
            self.media_list_loaded.emit((rows, total))

        run_coro(self.loop, _go(), error_label="load_media_list")

    def delete_media(self, channel_id: int, telegram_msg_id: int, media_idx: int) -> None:
        """单条 media 删除 — 后台 fire app.delete_media。"""

        async def _go() -> None:
            await self.app.delete_media(channel_id, telegram_msg_id, media_idx)

        run_coro(self.loop, _go(), error_label="delete_media")

    def delete_media_batch(self, items: list[tuple[int, int, int]]) -> None:
        """批量删除 — items: list[(channel_id, telegram_msg_id, media_idx)]。"""

        async def _go() -> None:
            for cid, mid, idx in items:
                try:
                    await self.app.delete_media(cid, mid, idx)
                except Exception:  # noqa: BLE001 — 单条失败不影响 batch
                    log.exception(
                        "delete_media batch failed: %s/%s/%d",
                        cid,
                        mid,
                        idx,
                    )

        run_coro(self.loop, _go(), error_label="delete_media_batch")

    def retry_media(self, channel_id: int, telegram_msg_id: int, media_idx: int) -> None:
        """单条 retry — 后台 fire app.retry_media。"""

        async def _go() -> None:
            await self.app.retry_media(channel_id, telegram_msg_id, media_idx)

        run_coro(self.loop, _go(), error_label="retry_media")

    def open_media(self, channel_id: int, telegram_msg_id: int, media_idx: int) -> None:
        """打开 media 文件 — 后台 fire app.open_media_with_result,失败时 emit
        open_media_failed 把原因给 UI(2026-08-25 v1.3.0 PR #5)。
        """

        async def _go() -> None:
            result = await self.app.open_media_with_result(
                channel_id,
                telegram_msg_id,
                media_idx,
            )
            if not result.success:
                reason = result.error or "未知原因"
                log.warning(
                    "open_media failed: channel=%s msg=%s idx=%d reason=%s",
                    channel_id,
                    telegram_msg_id,
                    media_idx,
                    reason,
                )
                self.open_media_failed.emit(
                    channel_id,
                    telegram_msg_id,
                    media_idx,
                    reason,
                )

        run_coro(self.loop, _go(), error_label="open_media")

    def reconcile_orphans(self, *, dry_run: bool) -> None:
        """孤儿 reconcile — dry_run=True 只 log,dry_run=False 真删。"""

        async def _go() -> None:
            await self.app.reconcile_orphans(dry_run=dry_run)

        run_coro(self.loop, _go(), error_label="reconcile_orphans")

    def preview_delete_by_channel(self, channel_id: int) -> None:
        """2026-08-25 v1.3.0 PR #8:Clear Channel dry-run — 后台 fire
        `app.preview_delete_by_channel` 后 emit `delete_preview_ready`。

        `app.preview_*` 严格只读,不会改 storage / objects 状态。
        MainWindow 收到 `delete_preview_ready` 后弹 `ClearChannelPreviewDialog`
        二次确认,用户勾上「我已了解不可撤销」才 enable OK,确认后走
        `vm.delete_by_channel(channel_id)` 真删。
        """

        async def _go() -> DeleteChannelPreview:
            return await self.app.preview_delete_by_channel(channel_id)

        def _on_success(preview: object) -> None:
            if isinstance(preview, DeleteChannelPreview):
                self.delete_preview_ready.emit(preview)

        run_coro(
            self.loop,
            _go(),
            on_success=_on_success,
            error_label="preview_delete_by_channel",
        )

    def delete_by_channel(self, channel_id: int) -> None:
        """2026-08-25 PR #4:批量删某频道所有 message — 后台 fire
        app.delete_by_channel。

        完成后 emit `channel_cleared(channel_id, deleted_count)` — UI 据此
        状态栏反馈 + 重新加载 media list。
        """

        async def _go() -> None:
            n = await self.app.delete_by_channel(channel_id)
            self.channel_cleared.emit(channel_id, n)

        run_coro(self.loop, _go(), error_label="delete_by_channel")

    def load_thumbnail(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
        media: object,
    ) -> None:
        """UI 行内请求加载缩略图(2026-08-25 PR #1)。

        流程:异步读 bytes(app.load_thumbnail_bytes)→ 主线程内
        QPixmap.fromData → emit thumbnail_loaded。失败(None bytes /
        非图像格式)on_success 拿到 None,UI 端保持 emoji 不变。
        """

        def _on_success(data: object) -> None:
            if not isinstance(data, (bytes, bytearray)) or not data:
                return
            # 2026-10-15 plan-batch6-2:VM 不再做 QPixmap 渲染 — payload
            # 改 `bytes`,widget 端 `_on_thumbnail_loaded` 自行调
            # `render_pixmap(bytes)` 生成 QPixmap,符合「VM 是 EventBus+signal
            # 适配器,不依赖 Qt 像素层」的分层约束。
            self.thumbnail_loaded.emit(
                channel_id,
                telegram_msg_id,
                media_idx,
                bytes(data),
            )

        async def _go() -> object:
            # media 是弱类型 object — VM 接口稳定,不依赖 DTO 强类型
            from tgmonitor.core.dto import MediaDTO

            if not isinstance(media, MediaDTO):
                return None
            # 2026-09-29:缩略图独立存储 — 必须传 (channel_id, msg_id,
            # media_idx) 三元组定位独立 thumbnails 表的 thumb 行。
            return await self.app.load_thumbnail_bytes(
                media,
                channel_id=channel_id,
                telegram_msg_id=telegram_msg_id,
                media_idx=media_idx,
            )

        run_coro(
            self.loop,
            _go(),
            on_success=_on_success,
            error_label="load_thumbnail",
        )

    async def load_media_bytes(self, media: object) -> bytes | None:
        """2026-08-31 v1.5.0 PR #A8:Lightbox 全屏预览 — 异步读原图 bytes。

        走 `app.load_media_bytes` 转发到 MediaService。
        """
        from tgmonitor.core.dto import MediaDTO

        if not isinstance(media, MediaDTO):
            return None
        return await self.app.load_media_bytes(media)
