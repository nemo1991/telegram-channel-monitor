"""MonitorViewModel — 把 EventBus 事件转 Qt signal(在 qasync 主线程安全更新 UI)。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from tgmonitor.core.config import Settings
from tgmonitor.core.dto import ChannelDTO, ExportRequest
from tgmonitor.core.events import (
    ChannelSubscribed,
    ChannelSyncDone,
    ChannelSyncProgress,
    ChannelUnsubscribed,
    ErrorOccurred,
    Event,
    EventBus,
    ExportDone,
    LoginStateChanged,
    MessageReceived,
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
    login_state = Signal(str)
    channels_changed = Signal()
    export_done = Signal(object, object)   # (result_dict | None, error | None)
    error = Signal(str)
    settings_changed = Signal(str, bool, str)  # (what, needs_relogin, backend_label)
    # 全量同步进度(sync dialog 订阅)
    sync_progress = Signal(object)         # ChannelSyncProgress
    sync_done = Signal(object)             # ChannelSyncDone(带 result)

    def __init__(
        self,
        app: AppService,
        monitor: MonitorService,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """存 3 个子系统引用 + 立即订阅 EventBus 所有相关事件。"""
        super().__init__()
        self.app = app
        self.monitor = monitor
        self.loop = loop
        self.known_channels: dict[int, ChannelDTO] = {}
        self._wire_bus()

    def _wire_bus(self) -> None:
        b: EventBus = self.app.bus
        b.subscribe(MessageReceived, self._on_message_received)
        b.subscribe(LoginStateChanged, self._on_login_state)
        b.subscribe(ChannelSubscribed, self._on_channel_subscribed)
        b.subscribe(ChannelUnsubscribed, self._on_channel_unsubscribed)
        b.subscribe(ExportDone, self._on_export_done)
        b.subscribe(ErrorOccurred, self._on_error)
        b.subscribe(SettingsChanged, self._on_settings_changed)
        b.subscribe(ChannelSyncProgress, self._on_sync_progress)
        b.subscribe(ChannelSyncDone, self._on_sync_done)

    # ---- EventBus → Qt signal 适配(都在主线程 loop 里被 await) ----

    async def _on_message_received(self, e: Event) -> None:
        if not isinstance(e, MessageReceived) or e.message is None:
            return
        # 直接 emit MessageDTO — 不要 asdict,会丢嵌套 MediaDTO 类型
        self.message_received.emit(e.message)

    async def _on_login_state(self, e: Event) -> None:
        if not isinstance(e, LoginStateChanged):
            return
        self.login_state.emit(e.state)

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
        backend_label = (
            f"DB={new.db_backend.value}, ObjectStore={new.objectstore_backend.value}"
        )
        self.settings_changed.emit(e.what, e.needs_relogin, backend_label)

    async def _on_sync_progress(self, e: Event) -> None:
        if not isinstance(e, ChannelSyncProgress):
            return
        self.sync_progress.emit(e)

    async def _on_sync_done(self, e: Event) -> None:
        if not isinstance(e, ChannelSyncDone):
            return
        self.sync_done.emit(e)

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

    def start_export(self, req: ExportRequest) -> None:
        """后台 fire `app.export(req)` 异步生成器,UI 不阻塞。

        `app.export` 内部 yield 心跳,这里只消耗掉(进度走 ExportProgress 事件)。
        """
        async def _go() -> None:
            async for _ in self.app.export(req):
                pass
        run_coro(self.loop, _go(), error_label="start_export")
