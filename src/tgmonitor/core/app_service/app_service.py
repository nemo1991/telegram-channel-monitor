"""AppService — 组合根 + 生命周期协调(不再是 god facade)。

2026-11-11 v2.0.0 Phase 3:745 行 god facade 拆为按域服务。C2 起域转发
全部删除,UI / CLI 直调域服务,本类只保留:
  - 组合根职责(构造 / 装配域服务)
  - 生命周期:bootstrap / start_monitor / stop_monitor / pause / resume /
    shutdown(跨 client / monitor / storage / objects,顺序敏感)
  - 热重载:reconfigure / validate_backends(涉及全体服务 backend swap)
  - 后端持有:client / storage / objects / downloader / monitor
    (property setter 传播给域服务)
  - 导出:export / export_media_list(每次新建轻量 ExportService)

域服务(UI 直接调 `app.<domain>.<method>`):
  - `auth`        AuthService        登录鉴权(get_login_state / submit_* / logout)
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

from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

from tgmonitor.core.auth_service import AuthService
from tgmonitor.core.batch_service import BatchService
from tgmonitor.core.channel_service import ChannelService
from tgmonitor.core.config import Settings
from tgmonitor.core.dto import ExportRequest, MediaExportRequest
from tgmonitor.core.events import EventBus
from tgmonitor.core.media_service import MediaService
from tgmonitor.core.message_service import MessageService
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream

if TYPE_CHECKING:
    from tgmonitor.core.monitor.downloader import MediaDownloader
    from tgmonitor.core.monitor.service import MonitorService


class AppService:
    """组合根 + 生命周期协调。域操作走 `auth/channels/messages/media/batch`。

    本类公开方法分三类:
    1. 生命周期(bootstrap / start_monitor / stop_monitor / pause_monitor /
       resume_monitor / shutdown)— 跨子系统协调,留组合根
    2. 热重载(reconfigure / validate_backends)— 全体服务 backend swap
    3. 导出(export / export_media_list)— 每次新建轻量 ExportService

    公开表面由 `tests/test_app_service_surface.py` allowlist 锁死 —
    新增域转发会被守护测试拒绝(域操作应加在域服务上)。
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

    # ---------- 生命周期 ----------

    async def bootstrap(self) -> tuple[str, str | None]:
        """应用启动调一次:session 有效直接 ready,无效走 login — 详见 `_lifecycle.bootstrap`。"""
        from tgmonitor.core.app_service._lifecycle import bootstrap as _bootstrap

        return await _bootstrap(self)

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

    async def shutdown(self) -> None:
        """app exit — 停 monitor + 关 client / storage / objects — 详见 `_lifecycle.shutdown`。"""
        from tgmonitor.core.app_service._lifecycle import shutdown as _shutdown

        await _shutdown(self)

    # ---------- 热重载 ----------

    async def reconfigure(self, new_settings: Settings) -> None:
        """用新 settings 重建 storage / objects — 详见 `_reconfigure.reconfigure`。"""
        from tgmonitor.core.app_service._reconfigure import reconfigure as _reconfigure

        await _reconfigure(self, new_settings)

    async def validate_backends(self, new_settings: Settings) -> None:
        """仅校验后端连通性,不切换运行时 — 详见 `_reconfigure.validate_backends`。"""
        from tgmonitor.core.app_service._reconfigure import validate_backends as _validate

        await _validate(self, new_settings)

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
