"""MonitorViewModel — 把 EventBus 事件转 Qt signal(在 qasync 主线程安全更新 UI)。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from tgmonitor.core.config import Settings
from tgmonitor.core.dto import (
    ChannelDTO,
    DeleteChannelPreview,
    ExportRequest,
    MediaDownloadStatus,
    MediaExportRequest,
    MediaType,
    SortDir,
    SortKey,
)
from tgmonitor.core.events import (
    ChannelSubscribed,
    ChannelSyncDone,
    ChannelSyncProgress,
    ChannelUnsubscribed,
    ConnectionStateChanged,
    ErrorOccurred,
    Event,
    EventBus,
    ExportDone,
    ExportProgress,
    LoginStateChanged,
    MediaDeleted,
    MediaDownloaded,
    MediaDownloadProgress,
    MediaReconcileFinished,
    MediaRetried,
    MessageEdited,
    MessageReceived,
    NotificationRequested,
    QuitRequested,
    SettingsChanged,
)
from tgmonitor.ui._async import run_coro

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)


class MonitorViewModel(QObject):
    """EventBus → Qt signal 适配层(VM 模式)。

    # 唯一职责:把 core 内部事件转 Qt signal,在 qasync 主线程 / 同一 loop
    # 安全更新 UI。不持有任何业务状态 — 业务状态在 `MonitorService._whitelist`
    # 和 `AppService`。
    #
    # `known_channels` 是 UI 缓存(VM 唯一例外):订阅后收到 `ChannelSubscribed`
    # 就更新,VM 用它在下栏显示频道名 — 不重新查 storage。
    #
    # 直接 emit MessageDTO 本身(不是 asdict)。
    # 原因:dataclasses.asdict() 会把嵌套的 MediaDTO 也转成 dict,
    # MainWindow 收到后 `MessageDTO(**dto_dict)` 不递归构回 MediaDTO,
    # MessageView._format 取 `med.type` 崩溃。Signal(object) 让 Qt 承载
    # Python 对象本身,跨线程在 qasync 同一 loop 下安全。
    """

    message_received = Signal(object)
    message_edited = Signal(object)  # 2026-08-24:编辑事件 MessageDTO
    media_downloaded = Signal(object)  # MediaDownloaded(下载结束,成功或失败)
    # 2026-09-01 v1.5.1 PR #B3:下载进度节流转发。`Signal(object)` 承载
    # `MediaDownloadProgress` 事件本身;UI 在 media_manager_widget.on_download_progress
    # 接到后查 row + 更新「已下载 X / Y (Z%)」文字。VM 不做节流(节流在
    # tdlib_client 端 0.5s/次已完成;UI 端 Qt 内置 coalesce 也够用)。
    media_download_progress = Signal(object)
    login_state = Signal(str)
    conn_state = Signal(
        str
    )  # TG 网络连接状态(waiting_for_network | connecting | updating | ready | unknown)
    channels_changed = Signal()
    export_done = Signal(object, object)  # (result_dict | None, error | None)
    # 导出进度(2026-08-30 v1.5.0 PR #A3)— payload ExportProgress
    # (request_id, written, total)。`total=None` 表示流式分页
    # (`service.py:_run_messages` 只在最后一批写完时才能确定总数)。
    export_progress = Signal(object)
    # 导出任务取消回调(2026-08-30 v1.5.0 PR #A3)— ExportProgressDialog
    # 取消按钮 → vm.cancel_current_export() → 取消当前 asyncio.Task。
    # payload (request_id,)
    export_cancelled = Signal(object)
    error = Signal(str)
    settings_changed = Signal(
        str, bool, bool, str
    )  # (what, needs_relogin, needs_restart, backend_label)
    # 全量同步进度(sync dialog 订阅)
    sync_progress = Signal(object)  # ChannelSyncProgress
    sync_done = Signal(object)  # ChannelSyncDone(带 result)
    # Media Manager(2026-08-24 新增)
    media_list_loaded = Signal(object)  # list_media 返回值(list of (msg, idx, media))
    media_retried = Signal(object)  # MediaRetried 转发
    media_deleted = Signal(object)  # MediaDeleted 转发
    media_reconcile_done = Signal(object)  # MediaReconcileFinished 转发
    # 按频道批量删除完成(2026-08-25 PR #4)— payload (channel_id, deleted_count)
    channel_cleared = Signal(int, int)
    # 缩略图加载完成(2026-08-25 PR #1)— payload (channel_id, telegram_msg_id,
    # media_idx, QPixmap)。bytes → QPixmap 已在 qasync 主线程做,QPixmap 跨信号
    # 安全(Qt metatype 自带);UI 收到直接 setPixmap。
    thumbnail_loaded = Signal(int, int, int, object)
    # 打开媒体失败(2026-08-25 v1.3.0 PR #5)— payload
    # (channel_id, telegram_msg_id, media_idx, reason)
    open_media_failed = Signal(int, int, int, str)
    # Clear Channel 预览就绪(2026-08-25 v1.3.0 PR #8)— payload
    # DeleteChannelPreview(只读)。MainWindow 接到后弹
    # `ClearChannelPreviewDialog` 二次确认,确认后才走 `vm.delete_by_channel`。
    delete_preview_ready = Signal(object)
    # 2026-08-30 v1.5.0 PR #A4:tray menu「退出」事件转发 — MainWindow
    # 接到 → qt_app.quit() 走 aboutToQuit → _shutdown_then_quit。
    # `pause=True` 路径不发此 signal(VM 内部 log 即可)。
    quit_requested = Signal()

    def __init__(
        self,
        app: AppService,
        monitor: MonitorService,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """存 3 个子系统引用 + 立即订阅 EventBus 所有相关事件。

        2026-08-30 v1.5.0 PR #A3:`_export_task` 持当前导出 asyncio.Task —
        `start_export` 创建,`cancel_current_export` 取消。`ExportDone`
        / 取消成功 / 异常结束都会清空此引用,避免下一次 export 误取消
        上一轮残留 task。
        """
        super().__init__()
        self.app = app
        self.monitor = monitor
        self.loop = loop
        self.known_channels: dict[int, ChannelDTO] = {}
        # 2026-08-30 v1.5.0 PR #A3:当前导出 future — UI 取消时 cancel 此 future
        # (run_coro 返 asyncio.Future,Future.cancel() 等同 Task.cancel())
        self._export_task: asyncio.Future[None] | None = None
        self._wire_bus()

    def _wire_bus(self) -> None:
        b: EventBus = self.app.bus
        b.subscribe(MessageReceived, self._on_message_received)
        b.subscribe(MessageEdited, self._on_message_edited)
        b.subscribe(MediaDownloaded, self._on_media_downloaded)
        # 2026-09-01 v1.5.1 PR #B3:下载进度节流转发 — TDLib 端 0.5s
        # 节流,VM 不再节流,直接 emit 给 UI(Qt coalesce 自动批量)。
        b.subscribe(MediaDownloadProgress, self._on_media_download_progress)
        b.subscribe(LoginStateChanged, self._on_login_state)
        b.subscribe(ConnectionStateChanged, self._on_conn_state)
        b.subscribe(ChannelSubscribed, self._on_channel_subscribed)
        b.subscribe(ChannelUnsubscribed, self._on_channel_unsubscribed)
        b.subscribe(ExportDone, self._on_export_done)
        # 2026-08-30 v1.5.0 PR #A3:导出进度订阅 — ExportService 每页写完
        # 发一次,UI 侧 ExportProgressDialog 接到 signal 刷新 QProgressBar。
        b.subscribe(ExportProgress, self._on_export_progress)
        b.subscribe(ErrorOccurred, self._on_error)
        b.subscribe(SettingsChanged, self._on_settings_changed)
        b.subscribe(ChannelSyncProgress, self._on_sync_progress)
        b.subscribe(ChannelSyncDone, self._on_sync_done)
        # Media Manager 转发(2026-08-24)
        b.subscribe(MediaRetried, self._on_media_retried)
        b.subscribe(MediaDeleted, self._on_media_deleted)
        b.subscribe(MediaReconcileFinished, self._on_media_reconcile_done)
        # 2026-08-30 v1.5.0 PR #A4:tray menu 退出事件 — MainWindow 接到
        # `quit_requested` signal 走 qt_app.quit → aboutToQuit → 干净退出。
        b.subscribe(QuitRequested, self._on_quit_requested)

    # ---- EventBus → Qt signal 适配(都在主线程 loop 里被 await) ----

    async def _on_message_received(self, e: Event) -> None:
        if not isinstance(e, MessageReceived) or e.message is None:
            return
        # 直接 emit MessageDTO — 不要 asdict,会丢嵌套 MediaDTO 类型
        self.message_received.emit(e.message)

    async def _on_message_edited(self, e: Event) -> None:
        """2026-08-24:TDLib updateMessageContent 来的编辑事件 → 转发 UI。

        UI 通过 message_edited 信号 → MessageView.replace_message 找到现有
        cell 重渲,不增删 row。
        """
        if not isinstance(e, MessageEdited) or e.message is None:
            return
        self.message_edited.emit(e.message)

    async def _on_media_downloaded(self, e: Event) -> None:
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

    async def _on_media_download_progress(self, e: Event) -> None:
        """2026-09-01 v1.5.1 PR #B3:`MediaDownloadProgress` 事件 → emit Qt signal。

        UI 端 media_manager_widget.on_download_progress 接到后查 row +
        更新「已下载 X / Y (Z%)」文字。节流已由 TDLib 客户端 0.5s/次
        完成,这里直接透传。
        """
        if not isinstance(e, MediaDownloadProgress):
            return
        self.media_download_progress.emit(e)

    async def _on_quit_requested(self, e: Event) -> None:
        """2026-08-30 v1.5.0 PR #A4:tray menu「退出」/「暂停监听」事件。

        `pause=True` 留作 v1.5.1,本 PR 不实现暂停逻辑(仅消费事件)。
        `pause=False` → emit Qt quit 信号,Qt 主循环走 aboutToQuit
        → _shutdown_then_quit → 真退出。
        """
        if not isinstance(e, QuitRequested):
            return
        if e.pause:
            log.info("QuitRequested(pause=True) — v1.5.1 实现暂停,本 PR 忽略")
            return
        # 真退出 — emit 到 main_window 持有 qt_app 信号;此处不便 import qt_app,
        # 改发 signal 通知 main_window 真退出
        self.quit_requested.emit()

    async def _on_login_state(self, e: Event) -> None:
        if not isinstance(e, LoginStateChanged):
            return
        self.login_state.emit(e.state)

    async def _on_conn_state(self, e: Event) -> None:
        if not isinstance(e, ConnectionStateChanged):
            return
        self.conn_state.emit(e.state)

    async def _on_channel_subscribed(self, e: Event) -> None:
        if not isinstance(e, ChannelSubscribed) or e.channel is None:
            return
        self.known_channels[e.channel.id] = e.channel
        self.monitor.add_to_whitelist(e.channel.id)
        self.channels_changed.emit()

    async def _on_channel_unsubscribed(self, e: Event) -> None:
        if not isinstance(e, ChannelUnsubscribed):
            return
        self.monitor.remove_from_whitelist(e.channel_id)
        self.channels_changed.emit()

    async def _on_export_done(self, e: Event) -> None:
        if not isinstance(e, ExportDone):
            return
        if e.error:
            self.export_done.emit(None, e.error)
        elif e.result is not None:
            self.export_done.emit(asdict(e.result), None)
        # 2026-08-30 v1.5.0 PR #A3:导出结束清空 task 引用 — 否则下一次
        # cancel_current_export 会误取消一个已完成 / 已抛错的 task
        # (asyncio 取消已完成 task 是 no-op,但日志噪音 + 引用悬挂)。
        self._export_task = None

    async def _on_export_progress(self, e: Event) -> None:
        """2026-08-30 v1.5.0 PR #A3:转发 ExportService 进度 → Qt signal。

        `total=None` 表示分页中(总数未知,QProgressBar 用 indeterminate);
        `total=int` 表示流式写盘结束,UI 收尾。
        """
        if not isinstance(e, ExportProgress):
            return
        self.export_progress.emit(e)

    async def _on_error(self, e: Event) -> None:
        if not isinstance(e, ErrorOccurred):
            return
        self.error.emit(f"[{e.source}] {e.message}")

    async def _on_settings_changed(self, e: Event) -> None:
        if not isinstance(e, SettingsChanged):
            return
        # events.py 里 SettingsChanged.new_settings: object | None(避免循环 import),
        # 这里 isinstance 窄化到 Settings,再用真字段拼 status 行。
        if not isinstance(e.new_settings, Settings):
            return
        new = e.new_settings
        backend_label = f"DB={new.db_backend.value}, ObjectStore={new.objectstore_backend.value}"
        self.settings_changed.emit(e.what, e.needs_relogin, e.needs_restart, backend_label)

    async def _on_sync_progress(self, e: Event) -> None:
        if not isinstance(e, ChannelSyncProgress):
            return
        self.sync_progress.emit(e)

    async def _on_sync_done(self, e: Event) -> None:
        if not isinstance(e, ChannelSyncDone):
            return
        self.sync_done.emit(e)

    # ---- Media Manager 事件转发(2026-08-24) ----

    async def _on_media_retried(self, e: Event) -> None:
        if not isinstance(e, MediaRetried):
            return
        self.media_retried.emit(e)

    async def _on_media_deleted(self, e: Event) -> None:
        if not isinstance(e, MediaDeleted):
            return
        self.media_deleted.emit(e)

    async def _on_media_reconcile_done(self, e: Event) -> None:
        if not isinstance(e, MediaReconcileFinished):
            return
        self.media_reconcile_done.emit(e)

    # ---- Media Manager UI 入口(2026-08-24) ----

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
                    log.exception("delete_media batch failed: %s/%s/%d", cid, mid, idx)

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
        """2026-08-25 PR #4:批量删某频道所有 message — 后台 fire app.delete_by_channel。

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

        流程:异步读 bytes(app.load_thumbnail_bytes)→ 主线程内 QPixmap.fromData
        → emit thumbnail_loaded。失败(None bytes / 非图像格式)on_success 拿到
        None,UI 端保持 emoji 不变。
        """
        from tgmonitor.ui.widgets.thumbnail_cache import render_pixmap

        def _on_success(data: object) -> None:
            if not isinstance(data, (bytes, bytearray)) or not data:
                return
            pix = render_pixmap(bytes(data))
            if pix is None:
                return
            self.thumbnail_loaded.emit(channel_id, telegram_msg_id, media_idx, pix)

        async def _go() -> object:
            # media 是弱类型 object — VM 接口稳定,不依赖 DTO 强类型
            from tgmonitor.core.dto import MediaDTO

            if not isinstance(media, MediaDTO):
                return None
            return await self.app.load_thumbnail_bytes(media)

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

    # ---- UI 主动调用 ----

    def bootstrap_ui(self) -> None:
        """MainWindow 构造后调一次:拉一次 joined 列表 + 通知 UI 刷新下栏。

        为什么需要:
        - bootstrap() 同步了 `_subscribed` 到内存,但 VM 不知道。
        - VM 的 `known_channels` 只在 `refresh_joined_channels` 或
          `ChannelSubscribed` 事件后才填充,启动时为空 → `_refresh_state`
          算 `subscribed` 时筛不出任何行 → 下栏一直空。
        - 这里的 refresh_joined_channels 同时也补了已知频道的元数据(title /
          username),下栏才能显示频道名而不是 "频道 -1001xxx"。

        已监听的 id 列表(monitor._whitelist)在 app._setup_async 里已经
        从 storage 读回并 set,这里只负责把 DTO 拉回来填 known_channels,
        然后 emit channels_changed 让 UI 算交集并刷新。
        """
        self.refresh_joined_channels()

    def refresh_joined_channels(self) -> None:
        """后台拉已加入频道列表 → 填 known_channels → emit channels_changed。

        # 走 `list_joined_channels` 是 best-effort UX 路径(不持久化),
        # 跟 `MonitorService._whitelist` 是两件事 — 后者是真理。
        """

        async def _go() -> None:
            chs = await self.app.list_joined_channels()
            for ch in chs:
                self.known_channels[ch.id] = ch
            self.channels_changed.emit()

        run_coro(self.loop, _go(), error_label="refresh_channels")

    def subscribe_channel(self, ch: ChannelDTO) -> None:
        """订阅频道 — 后台 fire `app.subscribe_channel`(同步入 storage + 发事件)。"""
        run_coro(self.loop, self.app.subscribe_channel(ch), error_label="subscribe_channel")

    def unsubscribe_channel(self, channel_id: int) -> None:
        """退订频道 — 后台 fire `app.unsubscribe_channel`(关订阅标志,保留历史)。"""

        async def _go() -> None:
            await self.app.unsubscribe_channel(channel_id)

        run_coro(self.loop, _go(), error_label="unsubscribe_channel")

    def load_recent_messages(self) -> None:
        """启动时拉最近 200 条已订阅频道消息 → emit message_received(填充 LIVE view)。"""

        async def _go() -> None:
            msgs = await self.app.list_messages(limit=200)
            for m in msgs:
                self.message_received.emit(m)

        run_coro(self.loop, _go(), error_label="load_recent_messages")

    def start_export(self, req: ExportRequest) -> asyncio.Future[None]:
        """后台 fire `app.export(req)` 异步生成器,UI 不阻塞。

        `app.export` 内部 yield 心跳,这里只消耗掉(进度走 ExportProgress
        事件)。2026-08-30 v1.5.0 PR #A3:返回 future 引用,存到
        `self._export_task` 供 `cancel_current_export` 用。返回 future
        而非 None 让 caller(本例 main_window)也能取句柄。
        """

        async def _go() -> None:
            async for _ in self.app.export(req):
                pass

        self._export_task = run_coro(self.loop, _go(), error_label="start_export")
        return self._export_task

    def cancel_current_export(self) -> None:
        """2026-08-30 v1.5.0 PR #A3:取消当前导出 — `asyncio.Task.cancel()`。

        走 CancelledError 路径,ExportService._run_messages 的 `async for`
        让出点会抛 CancelledError,ExportDone 仍发(带 error)。多次取消安全:
        第二次以后 task 已结束,no-op。
        """
        task = self._export_task
        if task is not None and not task.done():
            task.cancel()
            # 不立即清 _export_task — 让 _on_export_done 在 export 真正
            # 结束(CancelledError 被 run_coro 捕获)后清。避免下一次
            # start_export 进来时被 UI 误标「上一轮已取消」。

    def export_media_list(self, req: MediaExportRequest) -> None:
        """Media Manager 当前视图 → per-media CSV 导出 — 2026-08-25 v1.3.0 PR #7。

        复用 `ExportDone` 事件(`vm.export_done` 已绑),UI 无须新增信号;
        完成 / 失败通过 `_on_export_done` 走老路径弹消息框。
        """

        async def _go() -> None:
            async for _ in self.app.export_media_list(req):
                pass

        run_coro(self.loop, _go(), error_label="export_media_list")

    def export_zip(self, req: ExportRequest) -> None:
        """2026-09-01 v1.5.1 PR #B4:Media Manager 当前视图 → ZIP 打包。

        复用 `app.export(ExportRequest)` — `_run_messages` 检测到
        `format == ZIP` 时把 `object_store` 一律传进去(不依赖
        `include_thumbnails`),自动调 `ZipExporter.render` 完成打包。

        `req.format` 必须是 `ExportFormat.ZIP`;`req.channel_ids` 通常是
        `[<single_channel>]` 或 filter 出来的多条;`req.single_message_id`
        非 None 时走单条消息出口(见 `_run_messages`)。
        """

        async def _go() -> None:
            async for _ in self.app.export(req):
                pass

        run_coro(self.loop, _go(), error_label="export_zip")
