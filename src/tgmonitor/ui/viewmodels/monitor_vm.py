"""MonitorViewModel — 把 EventBus 事件转 Qt signal(在 qasync 主线程安全更新 UI)。

2026-10-20 plan-batch9-ε:5 mixin + BusMixin 拆分第五步 — media 域
下沉到 `_media.py:MediaMixin`(5 handler + 10 method + 10 signal,最大
块)。本 facade 多继承 `ChannelsMixin, SystemMixin, MessagesMixin,
MediaMixin, BusMixin, QObject`;`_wire_bus` 末尾 `super().wire_bus()`
让 BusMixin 协议接管 channels + system + messages + media 四域自动挂
(共 17 events)。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from tgmonitor.core.dto import (
    ChannelDTO,
    ExportRequest,
    MediaExportRequest,
)
from tgmonitor.core.events import (
    BatchDone,
    BatchProgress,
    Event,
    EventBus,
    ExportDone,
    ExportProgress,
)
from tgmonitor.ui.async_bridge import run_coro
from tgmonitor.ui.viewmodels._base import BusMixin
from tgmonitor.ui.viewmodels._channels import ChannelsMixin
from tgmonitor.ui.viewmodels._media import MediaMixin
from tgmonitor.ui.viewmodels._messages import MessagesMixin
from tgmonitor.ui.viewmodels._system import SystemMixin

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)


class MonitorViewModel(
    ChannelsMixin,
    SystemMixin,
    MessagesMixin,
    MediaMixin,
    BusMixin,
    QObject,
):
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

    # 2026-10-20 plan-batch9-ε:`media_downloaded` / `media_download_progress` /
    # `media_list_loaded` / `media_retried` / `media_deleted` /
    # `media_reconcile_done` / `thumbnail_loaded` / `open_media_failed` /
    # `delete_preview_ready` / `channel_cleared` 10 media signal 已下沉到
    # `_media.py:MediaMixin`(与 5 handler + 10 method + `_BUS_HANDLERS`
    # 5 events 一并搬)。
    login_state = Signal(str)
    conn_state = Signal(
        str
    )  # TG 网络连接状态(waiting_for_network | connecting | updating | ready | unknown)
    # 2026-10-19 plan-batch9-β:`channels_changed` signal 已下沉到
    # `_channels.py:ChannelsMixin`(2 handler + 4 method 一起搬)。
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
    # 2026-09-09 v1.7.2:批量操作进度/完成 — payload BatchProgress / BatchDone。
    # MainWindow 接到 → BatchProgressDialog 进度更新 / accept。
    batch_progress = Signal(object)
    batch_done = Signal(object)
    # 2026-08-30 v1.5.0 PR #A4:tray menu「退出」事件转发 — MainWindow
    # 接到 → qt_app.quit() 走 aboutToQuit → _shutdown_then_quit。
    # `pause=True` 路径不发此 signal(VM 内部 log 即可)。
    quit_requested = Signal()
    # 2026-09-03 v1.6.1:暂停 / 恢复监听 — payload str source("tray" 等)。
    # MainWindow 接到后:
    # - status bar label「⏸ 暂停」显示 / 隐藏
    # - window title 加 `(⏸ 暂停)` 后缀 / 取消
    # - tray icon 视觉由 tray_icon 自己监听 bus(MonitoringPaused/Resumed)
    monitoring_paused = Signal(str)
    monitoring_resumed = Signal(str)
    # 2026-10-19 plan-batch9-δ:messages 域 6 signal 已下沉到
    # `_messages.py:MessagesMixin`:`message_received` / `message_edited` /
    # `message_interactions_changed` / `message_search_results` /
    # `last_message_received` / `stats_changed`(后 2 个由 C-γ 在
    # SystemMixin 中归位到此处)。3 handler + 2 method 也同搬。
    # 2026-10-19 plan-batch9-γ:`selected_channel_changed` /
    # `backend_label_changed` 2 个系统子组件状态栏 signal 留 SystemMixin
    # (`last_message_received` / `stats_changed` 已归位到 MessagesMixin)。

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
        # 2026-10-01 v1.11.x 状态栏组件化:_message_count 累计当前 session
        # 收到的消息数(进程生命周期内)。restart 清零。状态栏 _StatsLabel
        # 通过 `stats_changed` signal 订阅。
        self._message_count = 0
        self._wire_bus()

    def _wire_bus(self) -> None:
        b: EventBus = self.app.bus
        # 2026-10-20 plan-batch9-ε:MediaDownloaded / MediaDownloadProgress /
        # MediaRetried / MediaDeleted / MediaReconcileFinished 已下沉到
        # MediaMixin._BUS_HANDLERS,BusMixin.wire_bus() 走 MRO walk 自动挂
        # (末尾 super().wire_bus() 调用)。
        b.subscribe(ExportDone, self._on_export_done)
        # 2026-08-30 v1.5.0 PR #A3:导出进度订阅 — ExportService 每页写完
        # 发一次,UI 侧 ExportProgressDialog 接到 signal 刷新 QProgressBar。
        b.subscribe(ExportProgress, self._on_export_progress)
        # 2026-09-09 v1.7.2:批量操作进度 / 完成 — AppService emit,
        # MainWindow 接到 signal 后 BatchProgressDialog 刷新进度。
        b.subscribe(BatchProgress, self._on_batch_progress)
        b.subscribe(BatchDone, self._on_batch_done)
        # 2026-10-20 plan-batch9-ε:BusMixin 协议接管已下沉 mixin 的 handler
        # 自动挂(ChannelsMixin 2 + SystemMixin 7 + MessagesMixin 3 +
        # MediaMixin 5 = 17 events);剩余 4 events(ExportDone / ExportProgress /
        # BatchProgress / BatchDone)仍由本 facade 手写,C-ζ 抽 ExportMixin
        # 时再下沉,C-η 收口时全 facade _wire_bus 应仅剩 1 行
        # super().wire_bus()。
        super().wire_bus()

    # ---- EventBus → Qt signal 适配(都在主线程 loop 里被 await) ----

    # 2026-10-19 plan-batch9-δ:`_on_message_received` / `_on_message_edited` /
    # `_on_message_interactions_changed` 3 个 messages handler 已下沉到
    # `_messages.py:MessagesMixin`(与 6 signal + 2 method + `_BUS_HANDLERS`
    # 3 events 一并搬)。

    # 2026-10-20 plan-batch9-ε:`_on_media_downloaded` / `_on_media_download_progress` /
    # `_on_media_retried` / `_on_media_deleted` / `_on_media_reconcile_done`
    # 5 个 media handler 已下沉到 `_media.py:MediaMixin`(与 10 signal +
    # 10 method + `_BUS_HANDLERS` 5 events 一并搬)。

    # 2026-10-19 plan-batch9-γ:`_on_quit_requested` / `_on_login_state` /
    # `_on_conn_state` / `_on_error` / `_on_settings_changed` /
    # `_on_sync_progress` / `_on_sync_done` 7 个 system handler 已下沉到
    # `_system.py:SystemMixin`。
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

    async def _on_batch_progress(self, e: Event) -> None:
        """2026-09-09 v1.7.2:转发 AppService batch 进度 → Qt signal。

        MainWindow 接到 → BatchProgressDialog 进度条 + 状态标签更新。
        """
        if not isinstance(e, BatchProgress):
            return
        self.batch_progress.emit(e)

    async def _on_batch_done(self, e: Event) -> None:
        """2026-09-09 v1.7.2:转发 AppService batch 完成 → Qt signal。

        MainWindow 接到 → BatchProgressDialog 收尾(accept / 标完成)。
        """
        if not isinstance(e, BatchDone):
            return
        self.batch_done.emit(e)

    # ---- UI 主动调用 ----

    # 2026-10-20 plan-batch9-ε:`load_media_list` / `delete_media` /
    # `delete_media_batch` / `retry_media` / `open_media` /
    # `reconcile_orphans` / `preview_delete_by_channel` / `delete_by_channel` /
    # `load_thumbnail` / `load_media_bytes` 10 个 media method 已下沉到
    # `_media.py:MediaMixin`(与 10 signal + 5 handler + `_BUS_HANDLERS`
    # 5 events 一并搬)。

    # 2026-10-19 plan-batch9-γ:`set_selected_channel` 已下沉到
    # `_system.py:SystemMixin`(与 4 statusbar signal 一起搬)。
    # 2026-10-19 plan-batch9-δ:本 mixin 阶段再校正 — statusbar signal
    # 剩 selected_channel_changed / backend_label_changed 2 个;
    # last_message_received / stats_changed 已归位 MessagesMixin。

    # 2026-10-19 plan-batch9-β:`bootstrap_ui` /
    # `refresh_subscribed_channels` / `subscribe_channel` /
    # `unsubscribe_channel` 4 method 已下沉到
    # `_channels.py:ChannelsMixin`(与 2 handler + `channels_changed`
    # signal + `known_channels` 字段一并搬)。

    # 2026-10-19 plan-batch9-δ:`load_recent_messages` / `search_messages`
    # 2 method 已下沉到 `_messages.py:MessagesMixin`(与 3 handler + 6 signal
    # + `_BUS_HANDLERS` 3 events 一并搬)。

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

    def cancel_current_batch(self) -> None:
        """2026-09-10 v1.7.3:取消当前批量操作 — `AppService._cancel_event.set()`。

        走协作式 cancel(不调 task.cancel):批量 facade 已在主 task loop
        await,设 Event 即可让循环跳出;task.cancel 会触发 CancelledError
        被 run_coro 吞,无法发 BatchDone(error='cancelled')。

        多次调用安全(只是 set 已 set 的 Event);无批量进行时也无副作用
        (下一次 facade 开头 `_cancel_event.clear()`)。已发 TDLib RPC 可能
        server-side 仍完成 — best-effort。
        """
        if self.app is not None:
            self.app.cancel_current_batch()

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
