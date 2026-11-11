"""AppService — 组合根 + 生命周期协调(不再是 god facade)。

2026-11-11 v2.0.0 Phase 3:745 行 god facade 拆为按域服务,本类只保留:
  - 组合根职责(构造 / 装配域服务)
  - 生命周期:bootstrap / start_monitor / stop_monitor / pause / resume /
    shutdown(跨 client / monitor / storage / objects,顺序敏感)
  - 热重载:reconfigure / validate_backends(涉及全体服务 backend swap)
  - 后端持有:client / storage / objects / downloader(property setter 传播)
  - 导出:export / export_media_list(每次新建轻量 ExportService)

域服务(UI 直接调,不再经本类转发 — 转发行将在 Phase 3-C2 删除):
  - `auth`        AuthService        登录鉴权
  - `channels`    ChannelService     订阅 / 退订 / 白名单
  - `messages`    MessageService     消息查询 / 收藏 / 标签 / 备注 / 单删
  - `media`       MediaService       媒体列表 / CRUD / 打开 / retry / reconcile
  - `batch`       BatchService       7 批量 op + 取消(自持 cancel event)
  - `channel_sync` ChannelSyncService 手动全量同步

UI 只调域服务方法,接收 DTO,监听 EventBus。
core 内部子系统(Monitor/Storage/ObjectStore/Export)不直接被 UI 引用。

生命周期:
    settings = Settings(); settings.ensure_dirs()
    bus = EventBus()
    storage = build_storage(settings); await storage.connect(); await storage.init_schema()
    objects = build_object_store(settings); await objects.connect()
    client = build_telegram_client(settings)
    app = AppService(bus, client, storage, objects, settings, monitor=monitor)
    # UI 启动时: app.bootstrap() / app.auth.submit_phone(...) / app.start_monitor()

热重载:调用 `reconfigure(new_settings)` 可切换 storage / objects(无需重启 app);
       TelegramClient / session / 鉴权状态变更需要登出再登入,UI 应引导。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

from tgmonitor.core.auth_service import AuthService
from tgmonitor.core.batch_service import BatchService
from tgmonitor.core.channel_service import ChannelService
from tgmonitor.core.config import MediaPolicy, Settings
from tgmonitor.core.dto import (
    ChannelDTO,
    CopyResult,
    DeleteChannelPreview,
    ExportRequest,
    MediaDownloadStatus,
    MediaDTO,
    MediaExportRequest,
    MediaType,
    MessageDTO,
    OpenMediaResult,
    RevealResult,
    SortDir,
    SortKey,
    SyncOptions,
    SyncResult,
)
from tgmonitor.core.events import EventBus, MediaReconcileFinished
from tgmonitor.core.media_service import MediaService
from tgmonitor.core.message_service import MessageService
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream

if TYPE_CHECKING:
    from tgmonitor.core.monitor.downloader import MediaDownloader
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)


class AppService:
    """组合根 + 生命周期协调。域操作走 `auth/channels/messages/media/batch`。

    本类公开方法分三类:
    1. 生命周期(bootstrap / start_monitor / stop_monitor / pause_monitor /
       resume_monitor / shutdown)— 跨子系统协调,留组合根
    2. 热重载(reconfigure / validate_backends)— 全体服务 backend swap
    3. 域转发(历史调用面,Phase 3-C2 删除)— UI 应直接调域服务
    """

    def __init__(
        self,
        bus: EventBus,
        client: TelegramClient,
        storage: StorageRepository,
        objects: ObjectStore,
        settings: Settings,
        monitor: MonitorService | None = None,
        env_path: Path | None = None,
    ) -> None:
        """装配域服务。`monitor` 由组合根(app.py / runtime.py)注入。

        `env_path`:pause/resume 持久化写 .env 用;缺失时仅 in-memory toggle。
        """
        self.bus = bus
        self.client = client
        self._storage: StorageRepository = storage
        self._objects: ObjectStore = objects
        self.settings = settings
        self._monitor: MonitorService | None = monitor
        # 内部状态(生命周期)
        self._update_streams: list[UpdateStream] = []
        self._running = False
        self._is_paused = settings.paused
        self.env_path: Path | None = env_path
        self._reconfiguring = False

        # 与 monitor 共享同一个 MediaDownloader(FULL 策略下 sync 复用);
        # 非 FULL 策略 / 未接线时 None,sync 跳过下载。
        self._downloader: MediaDownloader | None = (
            monitor.downloader if (monitor is not None) else None
        )

        # 域服务 — 构造时必建,生命周期 = AppService 本身。
        self.auth = AuthService(bus, client, settings)
        self.channels = ChannelService(bus, client, storage, monitor)
        self.messages = MessageService(bus, storage, monitor)
        self.media = MediaService(bus, storage, objects, downloader=self._downloader)
        # BatchService 自持 cancel event + 计时;paused 状态实时读(不持副本)。
        self.batch = BatchService(bus, client, monitor, is_paused=lambda: self._is_paused)

        # 全量同步服务(用户多选触发)— 延迟 import 避免循环
        from tgmonitor.core.channel_sync import ChannelSyncService

        self.channel_sync = ChannelSyncService(
            bus,
            client,
            storage,
            downloader=self._downloader,
            objects=objects,
            media_policy=settings.media_policy,
        )

    # ---------- 后端持有(property setter 传播给域服务)----------

    @property
    def monitor(self) -> MonitorService | None:
        """当前 MonitorService 引用(公开,UI 直接读)。"""
        return self._monitor

    @monitor.setter
    def monitor(self, value: MonitorService | None) -> None:
        """swap 时同步给 channels / messages / batch — 委托 `_backend_sync.set_monitor`。"""
        from tgmonitor.core.app_service._backend_sync import set_monitor

        set_monitor(self, value)

    @property
    def objects(self) -> ObjectStore:
        """当前对象存储后端引用(公开,UI 直接读)。"""
        return self._objects

    @objects.setter
    def objects(self, value: ObjectStore | None) -> None:
        """swap 时同步给 MediaService — 委托 `_backend_sync.set_objects`。"""
        from tgmonitor.core.app_service._backend_sync import set_objects

        set_objects(self, value)

    @property
    def storage(self) -> StorageRepository:
        """当前 storage 后端引用(公开,UI 直接读)。"""
        return self._storage

    @storage.setter
    def storage(self, value: StorageRepository) -> None:
        """swap 时同步给 channels / messages / media — 委托 `_backend_sync.set_storage`。"""
        from tgmonitor.core.app_service._backend_sync import set_storage

        set_storage(self, value)

    @property
    def downloader(self) -> MediaDownloader | None:
        """当前 MediaDownloader 引用(与 monitor 共享)。"""
        return self._downloader

    @downloader.setter
    def downloader(self, value: MediaDownloader | None) -> None:
        """swap 时同步给 media / channel_sync — 委托 `_backend_sync.set_downloader`。"""
        from tgmonitor.core.app_service._backend_sync import set_downloader

        set_downloader(self, value)

    # ---------- 鉴权(转发 AuthService)----------

    async def get_login_state(self) -> str:
        """当前登录状态机值(继承自 TelegramClient.state)。"""
        return self.client.state

    async def bootstrap(self) -> tuple[str, str | None]:
        """应用启动调一次:session 有效直接 ready,无效走 login — 详见 `_lifecycle.bootstrap`。"""
        from tgmonitor.core.app_service._lifecycle import bootstrap as _bootstrap

        return await _bootstrap(self)

    async def submit_phone(self, phone: str) -> tuple[str, str | None]:
        """委托给 `AuthService.submit_phone`。"""
        return await self.auth.submit_phone(phone)

    async def submit_code(self, code: str) -> tuple[str, str | None]:
        """委托给 `AuthService.submit_code`。"""
        return await self.auth.submit_code(code)

    async def submit_password(self, password: str) -> tuple[str, str | None]:
        """委托给 `AuthService.submit_password`。"""
        return await self.auth.submit_password(password)

    async def submit_email(self, email: str) -> tuple[str, str | None]:
        """委托给 `AuthService.submit_email`。"""
        return await self.auth.submit_email(email)

    async def submit_email_code(self, code: str) -> tuple[str, str | None]:
        """委托给 `AuthService.submit_email_code`。"""
        return await self.auth.submit_email_code(code)

    async def submit_registration(
        self, first_name: str, last_name: str = ""
    ) -> tuple[str, str | None]:
        """委托给 `AuthService.submit_registration`。"""
        return await self.auth.submit_registration(first_name, last_name)

    # ---------- 频道(转发 ChannelService)----------

    async def list_joined_channels(self) -> list[ChannelDTO]:
        """已加入 Telegram 频道(best-effort UX,不走 storage)。"""
        return await self.channels.list_joined_channels()

    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        """已订阅频道 — 单一真理走 storage。"""
        return await self.channels.list_subscribed_channels()

    async def subscribe_channel(self, channel: ChannelDTO) -> None:
        """转发 ChannelService.subscribe_channel。"""
        await self.channels.subscribe_channel(channel)

    async def unsubscribe_channel(self, channel_id: int) -> None:
        """转发 ChannelService.unsubscribe_channel。"""
        await self.channels.unsubscribe_channel(channel_id)

    def add_to_whitelist(self, channel_id: int) -> None:
        """转发 ChannelService.add_to_whitelist — sync。"""
        self.channels.add_to_whitelist(channel_id)

    def remove_from_whitelist(self, channel_id: int) -> None:
        """转发 ChannelService.remove_from_whitelist — sync。"""
        self.channels.remove_from_whitelist(channel_id)

    async def list_messages(
        self,
        channel_ids: list[int] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int | None = 200,
        search: str = "",
        include_unsubscribed: bool = False,
        favorite_only: bool = False,
        tag_only: bool = False,
        pinned_only: bool = False,
    ) -> list[MessageDTO]:
        """转发 MessageService.list_messages。"""
        return await self.messages.list_messages(
            channel_ids,
            date_from,
            date_to,
            limit,
            search=search,
            include_unsubscribed=include_unsubscribed,
            favorite_only=favorite_only,
            tag_only=tag_only,
            pinned_only=pinned_only,
        )

    # ---------- 同步(直接转 channel_sync)----------

    async def sync_channels(
        self,
        channel_ids: list[int],
        options: SyncOptions,
        *,
        media_policy: MediaPolicy | None = None,
    ) -> SyncResult:
        """全量同步 — UI 进度对话框经此调起。

        `media_policy` 非 None 时本轮覆盖 `channel_sync.media_policy`(CLI 用)。
        """
        return await self.channel_sync.sync_channels(
            channel_ids, options, media_policy=media_policy
        )

    # ---------- 消息流(实时)— 留 facade 维护 stream 列表 ----------

    def subscribe_updates(self) -> UpdateStream:
        """订阅实时更新流(转给 UI;关 app 时 stop_monitor 统一 aclose)。"""
        s = self.client.subscribe_updates()
        self._update_streams.append(s)
        return s

    async def start_monitor(self) -> None:
        """订阅 client 的实时更新并消费 — 详见 `_lifecycle.start_monitor`。"""
        from tgmonitor.core.app_service._lifecycle import start_monitor as _start_monitor

        await _start_monitor(self)

    async def stop_monitor(self) -> None:
        """停 monitor + 关所有 update stream;幂等 — 详见 `_lifecycle.stop_monitor`。"""
        from tgmonitor.core.app_service._lifecycle import stop_monitor as _stop_monitor

        await _stop_monitor(self)

    # ---------- 暂停 / 恢复 ----------

    @property
    def is_paused(self) -> bool:
        """当前是否处于暂停监听状态。"""
        return self._is_paused

    async def pause_monitor(self, source: str = "tray") -> None:
        """暂停监听 — 详见 `_lifecycle.pause_monitor`。幂等。"""
        from tgmonitor.core.app_service._lifecycle import pause_monitor as _pause_monitor

        await _pause_monitor(self, source=source)

    async def resume_monitor(self, source: str = "tray") -> None:
        """恢复监听 — 详见 `_lifecycle.resume_monitor`。幂等。"""
        from tgmonitor.core.app_service._lifecycle import resume_monitor as _resume_monitor

        await _resume_monitor(self, source=source)

    # ---------- 批量(转发 BatchService)----------

    def cancel_current_batch(self) -> None:
        """取消当前批量操作 — 转发 BatchService(set cancel event)。"""
        self.batch.cancel_current_batch()

    async def delete_messages_batch(self, items: list[tuple[int, int]]) -> int:
        """批量删 N 条消息 — 转发 BatchService。"""
        return await self.batch.delete_messages_batch(items)

    async def mark_messages_read(self, items: list[tuple[int, int]]) -> int:
        """批量标已读 — 转发 BatchService。"""
        return await self.batch.mark_messages_read(items)

    async def forward_messages(self, items: list[tuple[int, int]], to_chat_id: int) -> int:
        """批量转发 — 转发 BatchService。"""
        return await self.batch.forward_messages(items, to_chat_id)

    async def pin_messages(self, items: list[tuple[int, int]]) -> int:
        """批量钉选 — 转发 BatchService。"""
        return await self.batch.pin_messages(items)

    async def unpin_messages(self, items: list[tuple[int, int]]) -> int:
        """批量取消钉选 — 转发 BatchService。"""
        return await self.batch.unpin_messages(items)

    async def add_reaction(
        self, items: list[tuple[int, int]], emoji: str, *, is_big: bool = False
    ) -> int:
        """批量 emoji 回应 — 转发 BatchService。"""
        return await self.batch.add_reaction(items, emoji, is_big=is_big)

    async def remove_reaction(self, items: list[tuple[int, int]], emoji: str) -> int:
        """批量取消 emoji 回应 — 转发 BatchService。"""
        return await self.batch.remove_reaction(items, emoji)

    # ---------- 导出(每次新建轻量 ExportService)----------

    async def export(self, request: ExportRequest) -> AsyncIterator[None]:
        """yield 进度心跳(让 UI 不阻塞),正常结束或抛错。"""
        from tgmonitor.core.export.service import ExportService

        svc = ExportService(self.storage, self.objects, self.bus)
        async for _ in svc.run(request):
            yield

    async def export_media_list(
        self,
        request: MediaExportRequest,
    ) -> AsyncIterator[None]:
        """Media Manager 当前视图(per-media 行)导出。

        完成 / 失败走 `ExportDone` 事件(`vm.export_done` 已绑)。
        """
        from tgmonitor.core.export.service import ExportService

        svc = ExportService(self.storage, self.objects, self.bus)
        async for _ in svc.run(request):
            yield

    # ---------- Media Manager(转发 MediaService)----------

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
        """转发 MediaService.list_media。"""
        return await self.media.list_media(
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
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        """转发 MediaService.delete_media。"""
        await self.media.delete_media(channel_id, telegram_msg_id, media_idx)

    async def preview_delete_by_channel(
        self,
        channel_id: int,
    ) -> DeleteChannelPreview:
        """转发 MediaService.preview_delete_by_channel。"""
        return await self.media.preview_delete_by_channel(channel_id)

    async def delete_by_channel(self, channel_id: int) -> int:
        """转发 MediaService.delete_by_channel。"""
        return await self.media.delete_by_channel(channel_id)

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """单条消息删除 — 转发 MessageService(走 monitor 清理路径)。"""
        await self.messages.delete_message(channel_id, telegram_msg_id)

    async def retry_media(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        """重下 FAILED media — 转发 MediaService(downloader 经 setter 传播)。"""
        await self.media.retry_media(channel_id, telegram_msg_id, media_idx)

    async def load_thumbnail_bytes(
        self,
        media: MediaDTO,
        *,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> bytes | None:
        """转发 MediaService.load_thumbnail_bytes。"""
        return await self.media.load_thumbnail_bytes(
            media,
            channel_id=channel_id,
            telegram_msg_id=telegram_msg_id,
            media_idx=media_idx,
        )

    async def load_media_bytes(self, media: MediaDTO) -> bytes | None:
        """Lightbox 全屏预览 — 转发 MediaService.load_media_bytes。"""
        return await self.media.load_media_bytes(media)

    async def open_media(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> bool:
        """转发 MediaService.open_media。"""
        return await self.media.open_media(channel_id, telegram_msg_id, media_idx)

    async def open_media_with_result(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> OpenMediaResult:
        """转发 MediaService.open_media_with_result。"""
        return await self.media.open_media_with_result(channel_id, telegram_msg_id, media_idx)

    async def reveal_in_folder(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> RevealResult:
        """文件管理器高亮 media — 转发 MediaService.reveal_in_folder。"""
        return await self.media.reveal_in_folder(channel_id, telegram_msg_id, media_idx)

    async def copy_media_path(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> CopyResult:
        """转发 MediaService.copy_media_path。"""
        return await self.media.copy_media_path(channel_id, telegram_msg_id, media_idx)

    async def reconcile_orphans(self, *, dry_run: bool = True) -> MediaReconcileFinished:
        """转发 MediaService.reconcile_orphans。"""
        return await self.media.reconcile_orphans(dry_run=dry_run)

    # ---------- 消息标注(转发 MessageService)----------

    async def set_favorite(self, channel_id: int, telegram_msg_id: int, value: bool) -> None:
        """单条消息设收藏 — 转发 MessageService。"""
        await self.messages.set_favorite(channel_id, telegram_msg_id, value)

    async def set_tags(self, channel_id: int, telegram_msg_id: int, tags: list[str]) -> None:
        """单条消息设标签 — 转发 MessageService。"""
        await self.messages.set_tags(channel_id, telegram_msg_id, tags)

    async def set_notes(self, channel_id: int, telegram_msg_id: int, notes: str) -> None:
        """单条消息设备注 — 转发 MessageService。"""
        await self.messages.set_notes(channel_id, telegram_msg_id, notes)

    async def list_favorites(self) -> list[MessageDTO]:
        """列出所有收藏消息 — 转发 MessageService。"""
        return await self.messages.list_favorites()

    async def list_by_tag(self, tag: str) -> list[MessageDTO]:
        """按标签查消息 — 转发 MessageService。"""
        return await self.messages.list_by_tag(tag)

    # ---------- 关闭 / 热重载 ----------

    async def shutdown(self) -> None:
        """app exit — 停 monitor + 关 client / storage / objects — 详见 `_lifecycle.shutdown`。"""
        from tgmonitor.core.app_service._lifecycle import shutdown as _shutdown

        await _shutdown(self)

    async def reconfigure(self, new_settings: Settings) -> None:
        """用新 settings 重建 storage / objects — 详见 `_reconfigure.reconfigure`。"""
        from tgmonitor.core.app_service._reconfigure import reconfigure as _reconfigure

        await _reconfigure(self, new_settings)

    async def validate_backends(self, new_settings: Settings) -> None:
        """仅校验后端连通性,不切换运行时 — 详见 `_reconfigure.validate_backends`。"""
        from tgmonitor.core.app_service._reconfigure import validate_backends as _validate

        await _validate(self, new_settings)
