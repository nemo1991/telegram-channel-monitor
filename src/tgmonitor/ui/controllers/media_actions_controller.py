"""Media Manager + 媒体详情 banner 入口 controller。

2026-10-04 plan 阶段 3.3:从 `MainWindow` 抽 11 MediaManagerWidget signal
handler + 2 helper(clear_channel dialog / lightbox decoder) + `_qloop`
bug fix(reveal/copy 之前用 `self._qloop`,从未赋值,本 PR 改 `ctx.loop` 同
其它 handler 一致)。

设计:沿用 `LiveSelectionController` 模板(plain class + `MainWindowCtx`)。
翻译走 `QCoreApplication.translate("MainWindow", src)` 保留 i18n 上下文。

信号契约(全部连到 controller 公开方法):
- `media_manager.refresh_requested` → `on_media_refresh`
- `media_manager.{open,retry,delete}_requested` — 直接连 VM lambda,
  controller 不持方法(MainWindow 已有同款 lambda,无状态)
- `media_manager.{reveal,copy,batch_retry,batch_delete,prune,
  clear_channel,export_csv,export_zip,preview}_requested` → 9 入口
- `vm.open_media_failed` → `on_open_media_failed`
- `message_detail.preview_requested` → `on_media_preview`(与
  media_manager preview 走同一加载器)
"""

# mypy: disable-error-code="attr-defined"
# PySide6 6.6+.pyi 漏 QMessageBox.Yes/No 等 class-level const — 同
# main_window.py 顶层注释,统一关掉 attr-defined。

from __future__ import annotations

import logging
from typing import Callable, cast

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from tgmonitor.core.dto import (
    DeleteChannelPreview,
    ExportFormat,
    ExportRequest,
    MediaDownloadStatus,
    MediaExportRequest,
    MediaType,
    SortDir,
    SortKey,
)
from tgmonitor.core.events import MediaDeleted, MediaDownloaded
from tgmonitor.ui.async_bridge import run_coro
from tgmonitor.ui.controllers._base import MainWindowCtx
from tgmonitor.ui.widgets.lightbox_dialog import LightboxDialog, MediaItem

log = logging.getLogger(__name__)


class MediaActionsController:
    """Media Manager + 媒体详情入口 — 11 handler + 2 helper。"""

    def __init__(self, ctx: MainWindowCtx) -> None:
        self._ctx = ctx

    # ============================================================
    # 列表刷新 / 加载
    # ============================================================

    def on_media_refresh(self) -> None:
        """Media Manager 顶部 refresh / filter 变化 → 拉一次列表。

        同时把当前 known channels 推给 widget 的 channel 下拉框。
        """
        self._ctx.media_manager.set_known_channels(list(self._ctx.vm.known_channels.values()))
        f = self._ctx.media_manager.current_filters()
        self._ctx.vm.load_media_list(
            channel_id=f["channel_id"],
            status=f["status"],
            media_type=f["media_type"],
            search=f["search"],
        )

    # ============================================================
    # 批量操作(retry / delete / prune / clear channel)
    # ============================================================

    def on_media_batch_retry(self, keys: list) -> None:
        """批量 retry — keys 是 widget 内部的 _RowKey 列表(对象)。"""
        items = [(k.channel_id, k.telegram_msg_id, k.media_idx) for k in keys]

        async def _go() -> None:
            for cid, mid, idx in items:
                try:
                    await self._ctx.app.retry_media(cid, mid, idx)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "batch retry failed: %s/%s/%d",
                        cid,
                        mid,
                        idx,
                    )

        run_coro(self._ctx.loop, _go(), error_label="batch_retry_media")

    def on_media_batch_delete(self, keys: list) -> None:
        """批量 delete — 二次确认 + 调 VM.delete_media_batch。

        2026-09-28 fix(delete-no-feedback):之前末尾强制 `_on_media_refresh()`,
        但 `vm.delete_media_batch` 是 fire-and-forget(`run_coro` 调度 `_go`
        后立即返回),refresh 在 `_go` 跑完前就发了 — 拿到的 storage 还是旧
        数据。删除走完的真实 refresh 由 `_on_media_deleted` 的 100ms debounce
        timer 接续,本函数只需发请求、不必自己刷。
        """
        if not keys:
            return
        n = len(keys)
        ans = QMessageBox.warning(
            self._ctx.main_window,
            self._tr("删除确认"),
            self._tr(
                "确定删除选中的 %d 条媒体?\n"
                "删除后无法撤销;若 bytes 不再被引用,文件也会从对象存储删除。"
            )
            % n,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        items = [(k.channel_id, k.telegram_msg_id, k.media_idx) for k in keys]
        self._ctx.vm.delete_media_batch(items)

    def on_media_prune(self) -> None:
        """Media Manager 「Prune Orphans」按钮 — 二次确认 → reconcile(dry_run=False)。

        dry_run=False 会真删 ObjectStore bytes;UI 强警告。
        """
        ans = QMessageBox.warning(
            self._ctx.main_window,
            "Prune Orphans",
            self._tr("扫描对象存储并删除无引用的孤立文件。\n此操作不可撤销,确认执行?"),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        self._ctx.vm.reconcile_orphans(dry_run=False)

    def on_media_clear_channel(self, channel_id: int) -> None:
        """2026-08-25 v1.3.0 PR #8:Media Manager 「Clear Channel」按钮 —
        先 fire `vm.preview_delete_by_channel(channel_id)`,VM 后台读 storage
        后 emit `delete_preview_ready(DeleteChannelPreview)`,MainWindow
        接到后弹 `ClearChannelPreviewDialog`(必勾 ack 才 enable OK)。
        用户 OK 才走 `vm.delete_by_channel`;Cancel 不动。
        """
        self._ctx.vm.delete_preview_ready.connect(
            lambda preview, cid=channel_id: self._show_clear_channel_dialog(cid, preview),
        )
        self._ctx.vm.preview_delete_by_channel(channel_id)

    def _show_clear_channel_dialog(
        self,
        channel_id: int,
        preview: object,
    ) -> None:
        """`delete_preview_ready` 信号 handler — 弹 dialog,Accepted 才真删。"""

        # 断开一次性连接,避免重复触发
        try:
            self._ctx.vm.delete_preview_ready.disconnect()
        except (RuntimeError, TypeError):
            pass  # 没连过 / 已断

        if not isinstance(preview, DeleteChannelPreview):
            return
        ch = self._ctx.vm.known_channels.get(channel_id)
        title = ch.title if ch else ""
        from tgmonitor.ui.widgets.clear_channel_preview_dialog import (
            ClearChannelPreviewDialog,
        )

        dlg = ClearChannelPreviewDialog(preview, title, self._ctx.main_window)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._ctx.vm.delete_by_channel(channel_id)

    # ============================================================
    # 导出 CSV / ZIP(media manager 当前视图导出)
    # ============================================================

    def on_media_export_csv(self, out_path: str) -> None:
        """2026-08-25 v1.3.0 PR #7:Media Manager 「Export CSV」按钮 —
        构造 `MediaExportRequest`(透传 widget 当前 filter / sort / offset),
        调 `vm.export_media_list` 走 ExportService 异步生成器;完成 / 失败
        通过既有 `vm.export_done` 信号在 `_on_export_done` 弹消息框。
        """
        f = self._ctx.media_manager.current_filters()
        sort_val = f.get("sort")
        sort_dir_val = f.get("sort_dir")
        req = MediaExportRequest(
            channel_id=f.get("channel_id"),
            status=f.get("status"),
            media_type=f.get("media_type"),
            search=f.get("search", ""),
            sort=cast(SortKey, sort_val) if sort_val is not None else SortKey.DATE,
            sort_dir=(cast(SortDir, sort_dir_val) if sort_dir_val is not None else SortDir.DESC),
            out_path=out_path,
        )
        self._ctx.vm.export_media_list(req)

    def on_media_export_zip(self, out_path: str, include_thumbnails: bool) -> None:
        """2026-09-01 v1.5.1 PR #B4:Media Manager 「Export ZIP」按钮 —
        构造 `ExportRequest(format=ExportFormat.ZIP, include_thumbnails=...)`,
        调 `vm.export_zip` 走 ExportService 异步生成器。
        """
        f = self._ctx.media_manager.current_filters()
        # 把 `All Channels` (channel_id is None) → 空 list,跟 ExportRequest
        # 语义对齐(空 = 全频道)
        ch = f.get("channel_id")
        channel_ids = [int(ch)] if ch is not None else []
        req = ExportRequest(
            channel_ids=channel_ids,
            format=ExportFormat.ZIP,
            out_path=out_path,
            include_thumbnails=include_thumbnails,
            single_message_id=None,
        )
        self._ctx.vm.export_zip(req)

    # ============================================================
    # 打开失败 / 预览(Lightbox 三态分发)
    # ============================================================

    def on_open_media_failed(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
        reason: str,
    ) -> None:
        """2026-08-25 v1.3.0 PR #5:VM.open_media 失败 → QMessageBox.warning
        把 reason 显示给用户(v1.2.0 默默吞错,只能去 log 翻)。
        """
        QMessageBox.warning(
            self._ctx.main_window,
            self._tr("打开媒体失败"),
            self._tr("无法打开频道 #{cid} 消息 #{mid} 第 {idx} 个媒体:\n\n{reason}").format(
                cid=channel_id, mid=telegram_msg_id, idx=media_idx + 1, reason=reason
            ),
        )

    def on_media_preview(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        """2026-09-04 v1.6.7:Media Manager 缩略图点击 → Lightbox(支持 GIF / MP4)。

        流程:`app.storage.get_message` 找消息 → VM.load_media_bytes 异步读
        原图 bytes → 主线程按 mime 分三态:
            - `image/gif` → MediaItem.animated(QMovie 内联动画)
            - `image/*` / `application/octet-stream`(Sticker)— MediaItem.pixmap(QPixmap)
            - `video/mp4` → MediaItem.video(QMediaPlayer + QVideoWidget)
        其它类型(AUDIO/VOICE/DOCUMENT)→ 系统查看器 fallback。
        全程 async,UI handler 立即返回不阻塞 event loop。
        """

        async def _load_and_show() -> None:
            storage = self._ctx.app.storage
            if storage is None:
                return
            msg = await storage.get_message(channel_id, telegram_msg_id)
            if msg is None or media_idx >= len(msg.media):
                return
            med = msg.media[media_idx]
            mime = (med.mime_type or "").lower()
            fallback_fn = lambda: self._ctx.vm.open_media(  # noqa: E731 — bound lambda for QTimer
                channel_id, telegram_msg_id, media_idx
            )

            # 2026-09-04 v1.6.7:三态分发
            if med.type == MediaType.ANIMATION and mime.startswith("image/gif"):
                kind = "gif"
            elif med.type in (MediaType.VIDEO, MediaType.VIDEO_NOTE):
                kind = "video"
            elif med.type in (MediaType.PHOTO, MediaType.STICKER):
                kind = "image"
            else:
                # DOCUMENT / AUDIO / VOICE / 未知 → 系统查看器
                self._ctx.vm.open_media(channel_id, telegram_msg_id, media_idx)
                return

            data = await self._ctx.vm.load_media_bytes(med)
            if not data:
                QMessageBox.information(
                    self._ctx.main_window,
                    "Lightbox",
                    self._tr("无法加载预览(可能未下载或 backend 不可用)。\n文件:{name}").format(
                        name=med.file_name or "(no name)"
                    ),
                )
                return

            QTimer.singleShot(
                0,
                lambda: self._show_lightbox_items(
                    data=data,
                    kind=kind,
                    mime=mime,
                    title=med.file_name or "",
                    fallback_fn=fallback_fn,
                ),
            )

        run_coro(self._ctx.loop, _load_and_show(), error_label="media_preview")

    def _show_lightbox_items(
        self,
        *,
        data: bytes,
        kind: str,
        mime: str,
        title: str,
        fallback_fn: Callable[[], None],
    ) -> None:
        """2026-09-04 v1.6.7:bytes → MediaItem → LightboxDialog.showFullScreen。

        三态分发:image 走 QPixmap,gif 走 QMovie,video 走 QMediaPlayer。
        解码失败(QPixmap.loadFromData / QMovie.loadFromData)— 弹 warning。
        video codec miss 走 `fallback_fn`(通常是 vm.open_media)。
        """
        if kind == "gif":
            item = MediaItem(animated=data, mime_type=mime or "image/gif")
        elif kind == "video":
            item = MediaItem(video=data, mime_type=mime or "video/mp4")
        else:
            # image:bytes → QPixmap 走主线程 Qt 解码
            pix = QPixmap()
            if not pix.loadFromData(data):
                QMessageBox.warning(
                    self._ctx.main_window,
                    self._tr("Lightbox"),
                    self._tr("图片解码失败。"),
                )
                return
            item = MediaItem(pixmap=pix, mime_type=mime or "image/jpeg")

        dlg = LightboxDialog(
            items=[item],
            current=-1,
            title=title,
            fallback_fn=fallback_fn,
            data=data,  # v1.6.10:save-as 用
            source_title=title,  # v1.6.10:QFileDialog 默认文件名
        )
        dlg.showFullScreen()

    # ============================================================
    # Reveal / Copy 文件路径
    # ============================================================

    def on_media_reveal(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        """2026-08-27 v1.4.0 PR #16:Reveal in Folder — 调 AppService.reveal_in_folder,
        失败弹 QMessageBox.warning(同 Open 失败模式)。
        """

        # 2026-10-04 plan 阶段 3.3:原 `run_coro(self._qloop, ...)` 是 bug —
        # `self._qloop` 从未赋值,运行到会 AttributeError。统一走 `ctx.loop`。
        async def _go():
            return await self._ctx.app.reveal_in_folder(
                channel_id,
                telegram_msg_id,
                media_idx,
            )

        def _after(result):
            if result.success:
                return
            QMessageBox.warning(
                self._ctx.main_window,
                self._tr("Reveal 失败"),
                self._tr("无法在文件管理器中显示:\n\n{err}").format(err=result.error),
            )

        run_coro(
            self._ctx.loop,
            _go(),
            on_success=_after,
            error_label="reveal_in_folder",
        )

    def on_media_copy(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        """2026-08-27 v1.4.0 PR #16:Copy 路径 / URI — 调 AppService.copy_media_path,
        成功写剪贴板,失败弹 QMessageBox.warning。
        """

        async def _go():
            return await self._ctx.app.copy_media_path(
                channel_id,
                telegram_msg_id,
                media_idx,
            )

        def _after(result):
            if not result.success:
                QMessageBox.warning(
                    self._ctx.main_window,
                    self._tr("Copy 失败"),
                    self._tr("无法复制路径:\n\n{err}").format(err=result.error),
                )
                return
            # 写剪贴板
            QApplication.clipboard().setText(result.copied_value or "")

        run_coro(
            self._ctx.loop,
            _go(),
            on_success=_after,
            error_label="copy_media_path",
        )

    # ============================================================
    # VM 媒体事件回调(downloaded / retried / deleted)
    # ============================================================

    def on_media_downloaded(self, e) -> None:
        """媒体下载结束(成功/失败) → 实时流行与详情面板刷新状态。"""
        if not isinstance(e, MediaDownloaded) or e.media is None:
            return
        self._ctx.live_view.update_media_status(
            e.channel_id,
            e.telegram_msg_id,
            e.media,
        )
        self._ctx.message_detail.refresh_if_showing(e.channel_id, e.telegram_msg_id)
        # 活动指示器:成功/失败短促显示文件名;失败用 ⚠ 前缀
        fname = e.media.file_name or "?"
        if e.media.download_status == MediaDownloadStatus.DONE:
            self._ctx.status_bar.show_activity(f"已下载: {fname}", timeout_ms=1500)
        else:
            self._ctx.status_bar.show_activity(f"⚠ 下载失败: {fname}", timeout_ms=3000)
        # 2026-09-26 fix(retry-no-feedback):下载结束后刷新 Media Manager
        # 列表 — retry 路径走 background download_one,完成后 UI 看不到
        # 新状态;正常首次下载完成后 widget 也只显示进度文字,不显示
        # DONE,一并刷新列表让 row 显示正确状态。
        self._ctx.media_manager.refresh_requested.emit()

    def on_media_retried(self, e) -> None:
        """媒体被触发重下 → 立刻刷新 Media Manager 列表 + 状态栏左侧活动提示。

        之前 AppService.retry_media 只发 MediaRetried + MediaDownloaded 两个
        事件,Media Manager widget 都没订阅 — 用户点 Retry 后 row 状态不变
        (仍 FAILED),体感「点击无反应」。现在重试发起立刻 refresh widget,
        row 变 PENDING,状态栏左侧也提示「正在重试…」,视觉上能感知到点了。
        """
        # 状态栏左侧:短促提示。MediaRetried 没带 file_name(MediaDownloaded
        # 才带),无 storage round-trip 拉名字,文案简洁即可。
        self._ctx.status_bar.show_activity(self._tr("正在重试…"), timeout_ms=2000)
        self._ctx.media_manager.refresh_requested.emit()

    def on_media_deleted(self, e) -> None:
        """2026-09-28 fix(delete-no-feedback):Media Manager 媒体被删 → 状态栏短促
        提示 + 合并刷新 widget。

        之前 `vm.media_deleted` 事件无 UI 订阅,用户点 Delete 后 row 不消失、
        状态栏无提示 — 体感「点击无反应」。批量删除走 `vm.delete_media_batch`
        每条都触发 `MediaDeleted`,本 slot 自动覆盖:

        - 节流活动文案(`_throttle_activity("media_delete", …)`):批量 100 条
          时不会把 status bar 刷成「已删除 媒体 × 100」流水账。
        - `QTimer.start()` debounce 合并:连续 N 条 delete → 单次 widget
          refresh(定时器 interval=100ms,每次新 delete 重置计时,到点才真
          调 `media_manager.refresh_requested.emit()`)。
        """
        if not isinstance(e, MediaDeleted):
            return
        self._ctx.status_bar.throttle_activity(
            "media_delete",
            self._tr("已删除 媒体"),
            min_interval_ms=500,
            timeout_ms=1500,
        )
        # debounce:start() 在已运行的 timer 上自动重置 interval,批量
        # 100 条连续 delete 只触发 1 次 widget refresh(最后一次后 100ms)
        self._ctx.media_refresh_debounce.start()

    def flush_media_refresh_pending(self) -> None:
        """`media_refresh_debounce` 到点 — 实际发 widget refresh。

        `_media_refresh_debounce.timeout` 在 MainWindow `_setup_core`
        阶段连接(需 controller 实例化后立即连 — 同 SearchController /
        LiveSelectionController 模式,信号 wire 放 `_setup_controllers`)。
        """
        self._ctx.media_manager.refresh_requested.emit()

    # ============================================================
    # 翻译 helper
    # ============================================================

    def _tr(self, src: str) -> str:
        """翻译入口 — 保留 `MainWindow` context,沿用现有 zh_CN.ts / en_US.ts 翻译。

        原 MainWindow 自有 `self.tr(src)`,context = `MainWindow`;controller
        没 QWidget 引用,走 `QCoreApplication.translate` 全局入口。
        """
        return QCoreApplication.translate("MainWindow", src)
