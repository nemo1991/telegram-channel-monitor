"""AppService — UI 唯一入口门面。

2026-08-29 v1.5.0 PR #A2:从 1090 行单体 facade 拆为 facade + 3 个子 service
(MediaService / SubscriptionService / AuthService)。本类仅保留:
  - 组合根职责(构造 / 装配子 service)
  - bootstrap(401 rotate + client 重建 — 涉及 self.client swap)
  - start_monitor / stop_monitor(实时流生命周期)
  - shutdown(顺序敏感 — 关 monitor → client → storage → objects)
  - reconfigure / _rebuild_storage / _rebuild_objects / validate_backends
    (热重载 — 涉及 monitor.update_backends + ChannelSyncService 重建)
  - sync_channels(转 channel_sync)
  - 跨域状态:self.client / self.channel_sync / self.downloader / self.auth

转发原则:其它域方法 → 1 行 `await self._media.<x>` 或 `self._sub.<x>`。
公共方法签名 1:1 保留,UI 现有调用面不动。

UI 只调这个类的方法,接收 DTO,监听 EventBus。
core 内部子系统(Monitor/Storage/ObjectStore/Export)不直接被 UI 引用。

生命周期:
    settings = Settings()
    settings.ensure_dirs()
    bus = EventBus()
    storage = build_storage(settings); await storage.connect(); await storage.init_schema()
    objects = build_object_store(settings); await objects.connect()
    client = TdlibClient(...)  # 或 FakeTelegramClient()
    app = AppService(bus, client, storage, objects, settings)
    # UI 启动时: app.bootstrap() / app.login(...) / app.start_monitor()

热重载:调用 `reconfigure(new_settings)` 可切换 storage / objects(无需重启 app);
       TelegramClient / session / 鉴权状态变更需要登出再登入,UI 应引导。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, AsyncIterator

from tgmonitor.core.auth_service import AuthService
from tgmonitor.core.config import Settings
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
from tgmonitor.core.events import (
    ErrorOccurred,
    EventBus,
    MediaDownloaded,
    MediaRetried,
    SettingsChanged,
)
from tgmonitor.core.media_service import MediaService
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.objectstore.factory import build_object_store
from tgmonitor.core.settings_store import SettingsDiff, diff_settings
from tgmonitor.core.storage.factory import build_storage
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.subscription_service import SubscriptionService
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream

if TYPE_CHECKING:
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)


class AppService:
    """UI-facing facade.所有方法都是 async,接受/返回 DTO。

    大多数方法 1 行转发给子 service;鉴权转发给 AuthService;bootstrap /
    start_monitor / stop_monitor / shutdown / reconfigure / validate_backends
    留在此处(跨子域,跨 client / monitor / storage / objects 生命周期)。
    """

    def __init__(
        self,
        bus: EventBus,
        client: TelegramClient,
        storage: StorageRepository,
        objects: ObjectStore,
        settings: Settings,
        monitor: MonitorService | None = None,
    ) -> None:
        """5 个子系统引用 + 内部状态。`channel_sync` 延迟 import 避免循环。

        构造 3 个子 service:
        - AuthService(bus + client + settings)— 2026-08-03 微切抽出
        - SubscriptionService(bus + client + storage)— 2026-08-29 PR #A2 拆出
        - MediaService(bus + storage + objects + downloader)— 2026-08-29 PR #A2 拆出

        `monitor` 由组合根(app.py)注入(2026-08-18 修热重载):reconfigure
        要把新 storage/objects/settings 同步给 monitor,否则热重载切 PG 后
        monitor 仍写旧库,重启才生效。
        """
        self.bus = bus
        self.client = client
        # 2026-08-29 PR #A2:self.storage / self.objects 是 property,
        # set 时同步给 _sub / _media,避免测试 / reconfigure / 任何路径 swap
        # backend 后子 service 仍持旧引用,致 list_messages / reconcile /
        # open_media_with_result 等仍按旧 backend 分支走。
        # 显式类型注解让 mypy 知道 _storage / _objects 不可为 None —
        # 历史 facade 是非 None,保留向后兼容。`objects` property 返回
        # `| None` 是兼容旧 UI 路径(实际永远非 None)。
        self._storage: StorageRepository = storage
        self._objects: ObjectStore = objects
        self.settings = settings
        self.monitor = monitor
        # 内部状态
        self._update_streams: list[UpdateStream] = []
        self._running = False
        # 重入锁:reconfigure 期间阻止 save_message
        self._reconfiguring = False

        # 2026-08-24:与 monitor 共享同一个 MediaDownloader 实例(FULL 策略下
        # sync 也会复用做媒体下载);非 FULL 策略 / 未接线时传 None,sync 跳过下载。
        self.downloader = monitor.downloader if (monitor is not None) else None

        # 子 service(2026-08-29 PR #A2):Auth / Subscription / Media。
        # 构造时必建,生命周期 = AppService 本身;shutdown 时不动子 service
        # 引用,只调它们的 aclose / 关订阅标志。facade 方法直调不需 None 检查。
        # 2026-08-30 PR #A2 hotfix:类型从 `| None` 改为非 None — 上一版注解
        # 让 mypy 在所有 await self._sub.<m>() 处报 union-attr(16 处错)。
        self.auth = AuthService(bus, client, settings)
        self._sub: SubscriptionService = SubscriptionService(bus, client, storage)
        self._media: MediaService = MediaService(
            bus,
            storage,
            objects,
            downloader=self.downloader,
        )

        # 全量同步服务(用户多选触发)— 延迟初始化避免循环 import
        from tgmonitor.core.channel_sync import ChannelSyncService

        self.channel_sync = ChannelSyncService(
            bus,
            client,
            storage,
            downloader=self.downloader,
            objects=objects,
            media_policy=settings.media_policy,
        )

    # ---------- 鉴权(转发 AuthService) ----------

    async def get_login_state(self) -> str:
        """当前登录状态机值(继承自 TelegramClient.state)。"""
        return self.client.state

    # ---------- objects / storage 同步 property ----------

    @property
    def objects(self) -> ObjectStore:
        """当前对象存储后端引用(公开,UI 直接读)。"""
        return self._objects

    @objects.setter
    def objects(self, value: ObjectStore | None) -> None:
        """swap 时同步给 MediaService(否则 _media._objects 仍指向旧 backend)。

        触发场景:
        - 测试 `app.objects = fake_s3`
        - reconfigure / _rebuild_objects 末尾 `self.objects = new_objects`
        """
        if value is None:
            # 历史兼容:`aclose()` 后可置 None。MediaService 同步保留旧引用
            # (它会随 facade 一起销毁),不写回 None 避免触发 _media._objects 类型不匹配。
            return
        self._objects = value
        self._media._objects = value  # noqa: SLF001 — explicit sync contract

    @property
    def storage(self) -> StorageRepository:
        """当前 storage 后端引用(公开,UI 直接读)。"""
        return self._storage

    @storage.setter
    def storage(self, value: StorageRepository) -> None:
        """swap 时同步给 SubscriptionService + MediaService(否则子 service 仍指向旧 storage)。

        触发场景:
        - 测试 `app.storage = fake_repo`
        - reconfigure / _rebuild_storage 末尾 `self.storage = new_storage`
        """
        self._storage = value
        self._sub._storage = value  # noqa: SLF001 — explicit sync contract
        self._media._storage = value  # noqa: SLF001 — explicit sync contract

    async def bootstrap(self) -> tuple[str, str | None]:
        """应用启动时调用一次:自动检测本地 session,有效就直接 ready,无效走 login。

        返回 (state, detail)。

        401 加密 key 错误 → rotate + rebuild client + 再 start;client swap
        留在此方法(影响 `AppService.client`,AuthService 不该碰)。

        注意:历史上有 in-memory `self._subscribed` cache(2026-07-31 删),
        现在所有「订阅真理」都走 `storage.list_subscribed_channels()` —
        详见 `docs/SUBSCRIBED_DRIFT_ANALYSIS.md` #A/#B/#C。
        """

        try:
            state, detail = await self.client.start()
        except Exception as e:  # noqa: BLE001
            await self.bus.publish(ErrorOccurred(source="bootstrap", message=str(e), exception=e))
            return "error", str(e)
        # 如果 start 失败 + 检测到 401 → 让底层的 nuke_and_rebuild 接管,
        # rotate 加密 key + 重建 client + 再 start 一次
        if state == "error" and detail and "encryption key" in detail:
            log.warning("bootstrap: 401 detected — rotating key and rebuilding client")
            await self.client.nuke_and_rebuild(rotate_key=True)
            from tgmonitor.core.telegram.factory import build_telegram_client

            await self.client.close()
            self.client = build_telegram_client(
                self.settings,
                use_fake=False,
                event_bus=self.bus,
            )
            # client swap 后必须重建 SubscriptionService(它持有旧 client)
            self._sub = SubscriptionService(self.bus, self.client, self.storage)
            # AuthService 持旧 client 引用,重建
            self.auth = AuthService(self.bus, self.client, self.settings)
            state, detail = await self.client.start()
        # client 端已经 publish 过 LoginStateChanged,这里只 fail-safe 再发一次终态
        if state == "error":
            await self.bus.publish(
                ErrorOccurred(
                    source="bootstrap",
                    message=detail or "start failed",
                )
            )
        return state, detail

    async def submit_phone(self, phone: str) -> tuple[str, str | None]:
        """委托给 `AuthService.submit_phone`(2026-08-03 微切抽出,统一失败路径)。"""
        return await self.auth.submit_phone(phone)

    async def submit_code(self, code: str) -> tuple[str, str | None]:
        """委托给 `AuthService.submit_code`。"""
        return await self.auth.submit_code(code)

    async def submit_password(self, password: str) -> tuple[str, str | None]:
        """委托给 `AuthService.submit_password`。"""
        return await self.auth.submit_password(password)

    async def submit_email(self, email: str) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:委托给 `AuthService.submit_email`。"""
        return await self.auth.submit_email(email)

    async def submit_email_code(self, code: str) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:委托给 `AuthService.submit_email_code`。"""
        return await self.auth.submit_email_code(code)

    async def submit_registration(
        self, first_name: str, last_name: str = ""
    ) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:委托给 `AuthService.submit_registration`。"""
        return await self.auth.submit_registration(first_name, last_name)

    # ---------- 频道(转发 SubscriptionService) ----------

    async def list_joined_channels(self) -> list[ChannelDTO]:
        """已加入 Telegram 频道(best-effort UX,不走 storage)。"""
        return await self._sub.list_joined_channels()

    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        """已订阅频道 — 转发 SubscriptionService(单一真理走 storage)。"""
        return await self._sub.list_subscribed_channels()

    async def subscribe_channel(self, channel: ChannelDTO) -> None:
        """转发 SubscriptionService.subscribe_channel。"""
        await self._sub.subscribe_channel(channel)

    async def unsubscribe_channel(self, channel_id: int) -> None:
        """转发 SubscriptionService.unsubscribe_channel。"""
        await self._sub.unsubscribe_channel(channel_id)

    async def list_messages(
        self,
        channel_ids: list[int] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int | None = 200,
        search: str = "",
    ) -> list[MessageDTO]:
        """转发 SubscriptionService.list_messages(2026-09-01 v1.5.1 PR #B2 加 `search`)。"""
        return await self._sub.list_messages(channel_ids, date_from, date_to, limit, search=search)

    # ---------- 同步(直接转 channel_sync) ----------

    async def sync_channels(
        self,
        channel_ids: list[int],
        options: SyncOptions,
    ) -> SyncResult:
        """全量同步 — UI 进度对话框经此调起。

        `options` 用 dataclass,UI 端构造(delay_ms 等覆盖 Settings 默认值)。
        """
        return await self.channel_sync.sync_channels(channel_ids, options)

    # ---------- 消息流(实时)— 留 facade 维护 stream 列表 ----------

    def subscribe_updates(self) -> UpdateStream:
        """订阅实时更新流(转给 UI;关 app 时 stop_monitor 统一 aclose)。"""
        s = self._sub.subscribe_updates()
        self._update_streams.append(s)
        return s

    async def start_monitor(self) -> None:
        """订阅 client 的实时更新并消费(MonitorService 的细节后续接入)。"""
        if self._running:
            return
        self._running = True

    async def stop_monitor(self) -> None:
        """停 monitor + 关所有 update stream;幂等。"""
        self._running = False
        for s in self._update_streams:
            try:
                await s.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._update_streams.clear()

    # ---------- 导出(由 ExportService 提供实现)— 每次新建轻量 svc ----------

    async def export(self, request: ExportRequest) -> AsyncIterator[None]:
        """yield 进度心跳(让 UI 不阻塞),正常结束或抛错。"""
        from tgmonitor.core.export.service import ExportService

        # 2026-08-30 PR #A2 hotfix:self.storage / self.objects 类型是
        # `| None`(property 兼容旧 UI),但 ExportService 构造函数要求
        # 非 None — UI 启动后到 export 调用期间 facade 必持有真 backend,
        # 此处用 assert 锁死契约。
        assert self.storage is not None and self.objects is not None
        svc = ExportService(self.storage, self.objects, self.bus)
        async for _ in svc.run(request):
            yield

    async def export_media_list(
        self,
        request: MediaExportRequest,
    ) -> AsyncIterator[None]:
        """Media Manager 当前视图(per-media 行)导出 — 2026-08-25 v1.3.0 PR #7。

        `MediaExportRequest` 与 `ExportRequest` 是完全独立的 dataclass,
        字段语义对应 `storage.list_media`(filter + sort + offset);走
        ExportService 内部 isinstance 调度。UI 端 fire-and-forget 即可,
        完成 / 失败走 `ExportDone` 事件(`vm.export_done` 已绑)。
        """
        from tgmonitor.core.export.service import ExportService

        assert self.storage is not None and self.objects is not None
        svc = ExportService(self.storage, self.objects, self.bus)
        async for _ in svc.run(request):
            yield

    # ---------- Media Manager(转发 MediaService)— 2026-08-29 PR #A2 拆出 ----

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
        return await self._media.list_media(
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
        await self._media.delete_media(channel_id, telegram_msg_id, media_idx)

    async def preview_delete_by_channel(
        self,
        channel_id: int,
    ) -> DeleteChannelPreview:
        """转发 MediaService.preview_delete_by_channel。"""
        return await self._media.preview_delete_by_channel(channel_id)

    async def delete_by_channel(self, channel_id: int) -> int:
        """转发 MediaService.delete_by_channel。"""
        return await self._media.delete_by_channel(channel_id)

    async def retry_media(
        self,
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

        msg = await self.storage.get_message(channel_id, telegram_msg_id)
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
        await self.storage.update_message(new_msg)
        # 清旧 bytes — 让 download_one(force=True) 一定走真下载路径
        if old_object_key and self.objects is not None:
            try:
                await self.objects.delete(old_object_key)
            except Exception:  # noqa: BLE001
                log.warning(
                    "retry pre-clean bytes %s failed",
                    old_object_key,
                    exc_info=True,
                )
        # 先发 MediaRetried,UI 立刻把状态切到 PENDING(避免用户重复点 Retry)
        await self.bus.publish(
            MediaRetried(
                channel_id=channel_id,
                telegram_msg_id=telegram_msg_id,
                media_idx=media_idx,
            )
        )
        # 然后走同步下载路径(retry 走 facade 直调,不走 worker queue —
        # 2026-08-24 D4:不增加 force flag 进 queue,避免协议变更)
        if self.downloader is not None:
            try:
                updated = await self.downloader.download_one(
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
            await self.storage.update_message(final_msg)
            await self.bus.publish(
                MediaDownloaded(
                    channel_id=channel_id,
                    telegram_msg_id=telegram_msg_id,
                    media=updated,
                )
            )

    async def load_thumbnail_bytes(self, media: MediaDTO) -> bytes | None:
        """转发 MediaService.load_thumbnail_bytes。"""
        return await self._media.load_thumbnail_bytes(media)

    async def load_media_bytes(self, media: MediaDTO) -> bytes | None:
        """2026-08-31 v1.5.0 PR #A8:Lightbox 全屏预览转发 — 读原图 bytes。"""
        return await self._media.load_media_bytes(media)

    async def open_media(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> bool:
        """转发 MediaService.open_media(向后兼容 wrapper)。"""
        return await self._media.open_media(channel_id, telegram_msg_id, media_idx)

    async def open_media_with_result(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> OpenMediaResult:
        """转发 MediaService.open_media_with_result。"""
        return await self._media.open_media_with_result(channel_id, telegram_msg_id, media_idx)

    async def reveal_in_folder(
        self,
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

        msg = await self.storage.get_message(channel_id, telegram_msg_id)
        if msg is None or media_idx >= len(msg.media):
            return RevealResult(False, "消息或媒体不存在")
        med = msg.media[media_idx]
        if not med.object_key or med.download_status != MediaDownloadStatus.DONE:
            return RevealResult(False, "媒体未下载完成")
        if isinstance(self.objects, S3ObjectStore):
            return RevealResult(
                False,
                "S3 后端无本地路径:请使用「Copy 路径」拿到 s3:// URI",
            )
        if not isinstance(self.objects, (LocalObjectStore, FolderObjectStore)):
            return RevealResult(
                False,
                f"不支持的对象存储后端: {type(self.objects).__name__}",
            )

        try:
            abs_path = self.objects._path(med.object_key)  # noqa: SLF001
            if not abs_path.exists():
                return RevealResult(False, f"文件不存在: {abs_path}")
            # OS-specific 唤起文件管理器(macOS `open -R` 高亮 / Windows
            # `explorer /select,` / Linux `xdg-open <parent_dir>`)
            await _asyncio.to_thread(
                self._spawn_reveal,
                abs_path,
                _sys.platform,
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
        """转发 MediaService.copy_media_path。"""
        return await self._media.copy_media_path(channel_id, telegram_msg_id, media_idx)

    async def reconcile_orphans(self, *, dry_run: bool = True):
        """转发 MediaService.reconcile_orphans。"""
        return await self._media.reconcile_orphans(dry_run=dry_run)

    # ---------- 关闭 ----------

    async def shutdown(self) -> None:
        """app exit — 停 monitor + 关 client / storage / objects(顺序敏感)。

        2026-08-30 PR #A2 hotfix:不再清空 self._sub / self._media 引用 —
        它们是构造时必建,与 facade 同生命周期,置 None 反而让 mypy 在后续
        方法(若被误调)报 union-attr。shutdown 后整个 facade 随 app 销毁,
        GC 时一起回收。子 service 持旧 storage/objects 引用也无害
        (close() 后该引用已不可用,但 facade 已不再有调用入口)。
        """
        await self.stop_monitor()
        # 关 TelegramClient (停 tdlib_json 的 updates_loop + tdjson 子进程)
        try:
            await self.client.close()
        except Exception:  # noqa: BLE001
            log.exception("client.close() failed")
        await self.storage.close()
        await self.objects.close()

    # ---------- 热重载 ----------

    async def reconfigure(self, new_settings: Settings) -> None:
        """用新 settings 重建 storage / objects(不重建 TelegramClient)。

        Telegram 凭据(api_id/api_hash/phone)若变化,needs_relogin=True(UI 引导登出登入);
        proxy / session_dir 若变化,needs_restart=True(TdlibClient 构造参数,运行时
        不重建,UI 提示需重启生效 — 2026-08-18,此前纯代理/会话目录变更 diff 判
        无变化直接 return,UI 却弹「已保存并热重载」,属假成功)。

        流程(2026-08-03 微切,2026-08-18 加后端同步 + needs_restart):
          1. `diff_settings` 算 needs_relogin / storage_changed / objects_changed /
             client_changed
          2. 无变化 → return
          3. storage 优先(在 hot path)→ `_rebuild_storage`
          4. objects → `_rebuild_objects`(2026-08-18 起**无条件**跑 — 见下)
          5. 把新 storage/objects/settings 同步给 monitor(含重建下载器 +
             重载白名单)、重建 channel_sync — 否则热重载切 PG 不生效
          6. 重建 SubscriptionService / MediaService(它们持旧 storage / objects)
          7. publish `SettingsChanged` + 提交新 settings

        `_rebuild_objects` 无条件执行的背景:之前只在 `objects_changed` 时重建
        校验,若坏的对象存储配置已躺在 .env、本轮保存恰好没改对象存储字段,
        diff 判 objects 未变 → 跳过校验 → 静默通过,直到写 media 才报
        `S3 API Requests must be made to API port`。现在只要有任何设置变化
        (含纯凭据 / 纯代理变化),都真实 connect 校验一次对象存储,失败上抛、
        settings 不提交。
        """
        diff = diff_settings(self.settings, new_settings)
        if not diff.changed:
            return  # 无变化

        new_settings.ensure_dirs()

        if diff.storage_changed:
            await self._rebuild_storage(new_settings)
        # 无条件校验对象存储(配置即使没变也真实 connect 校验)
        await self._rebuild_objects(new_settings)

        # 5) 把新后端同步给所有持引用者 —— reconfigure 只换 AppService 自己
        # 的引用时,monitor / MediaDownloader 仍持旧 storage,实时 / 补拉消息
        # 继续写旧库,用户看到"切 PG 没生效,重启才生效"(2026-08-18 修)。
        if self.monitor is not None:
            await self.monitor.update_backends(
                self.storage,
                self.objects,
                new_settings,
            )
            # 2026-08-24:reconfigure 后 monitor 重建了 MediaDownloader(新
            # storage / objects / max_bytes),同步给 self.downloader 让 sync
            # 也用新实例。policy 变化同样需要 — 直接拿新 settings 字段。
            self.downloader = self.monitor.downloader
        from tgmonitor.core.channel_sync import ChannelSyncService

        self.channel_sync = ChannelSyncService(
            self.bus,
            self.client,
            self.storage,
            downloader=self.downloader,
            objects=self.objects,
            media_policy=new_settings.media_policy,
        )

        # 6) 重建子 service(它们持旧 storage / objects 引用,必须跟着切)
        self._sub = SubscriptionService(self.bus, self.client, self.storage)
        self._media = MediaService(
            self.bus,
            self.storage,
            self.objects,
            downloader=self.downloader,
        )

        # 7) 提交新 settings + 事件
        self.settings = new_settings
        # AuthService 持旧 settings 引用的话,_check_credentials 预检用旧凭据
        # (2026-08-18:reconfigure 后重建,消除引用不一致)
        self.auth = AuthService(self.bus, self.client, new_settings)
        await self.bus.publish(
            SettingsChanged(
                what=_what_label(diff),
                new_settings=new_settings,
                needs_relogin=diff.needs_relogin,
                needs_restart=diff.client_changed,  # proxy / session_dir 变更需重启生效
            )
        )

    async def _rebuild_storage(self, new_settings: Settings) -> None:
        """切换 storage:**先建新库**(connect + init_schema)成功后才关旧库。

        失败时异常上抛(`reconfigure` 中止、settings 不提交),且旧 storage
        保持可用 — 不能"先关后建":新库连不上时旧存储已 close,monitor 会
        写进已关闭的 store,数据静默丢失(2026-08-13 修,PG 连不上时的表现)。
        """
        self._reconfiguring = True
        try:
            new_storage = build_storage(new_settings)
            try:
                await new_storage.connect()
                await new_storage.init_schema()
            except BaseException:
                # 新库未就绪:关掉已建连接再上抛,不留泄漏;旧 storage 不动
                try:
                    await new_storage.close()
                except Exception:  # noqa: BLE001
                    log.exception("关闭未就绪的新 storage 失败")
                raise
            # 新库就绪后才关旧库并替换;关旧库失败只 log,不影响切换
            try:
                await self.storage.close()
            except Exception as e:  # noqa: BLE001
                log.warning("关闭旧 storage 失败: %s", e)
            self.storage = new_storage
            # 2026-07-31 修 SUBSCRIBED_DRIFT_ANALYSIS #C 收尾:之前
            # 这里 `list_channels()`(全频道)跟 in-memory `_subscribed`
            # cache 做 union,会把 unsubscribed 旧频道错误标记为"已订"。
            # `AppService._subscribed` cache 已整体删除,真理 = storage。
            # VM 通过 `list_subscribed_channels()` 在 init_schema 之后
            # 重新拉一遍,自动同步到 monitor / channel_widget。
            # 触发路径:`reconfigure` 末尾 `await self.bus.publish(
            # SettingsChanged(..., needs_relogin=False))` → VM 的
            # `_on_settings_changed` slot 会重新走 bootstrap。
        finally:
            self._reconfiguring = False

    async def _rebuild_objects(self, new_settings: Settings) -> None:
        """切换 objectstore:先建新、成功后关旧(与 `_rebuild_storage` 同语义)。

        `connect()` 失败(如 S3 端点 / 凭据 / 权限错)异常上抛,reconfigure
        中止、settings 不提交、旧 objectstore 保持可用;新建连接被关闭不留泄漏
        (2026-08-18,与 _rebuild_storage 对齐)。
        """
        new_objects = build_object_store(new_settings)
        try:
            await new_objects.connect()
        except BaseException:
            try:
                await new_objects.close()
            except Exception:  # noqa: BLE001
                log.exception("关闭未就绪的新 objectstore 失败")
            raise
        try:
            await self.objects.close()
        except Exception as e:  # noqa: BLE001
            log.warning("关闭旧 objectstore 失败: %s", e)
        self.objects = new_objects

    async def validate_backends(self, new_settings: Settings) -> None:
        """仅校验后端连通性,**不切换运行时** — 供「仅保存到 .env」按钮用。

        storage:connect → init_schema → close;objectstore:connect → close。
        任一失败上抛,调用方据此放弃落盘 .env;成功时 `self.storage` /
        `self.objects` 完全不受影响。校验中新建的连接失败即关闭,不留泄漏
        (与 `_rebuild_storage` / `_rebuild_objects` 同一套失败清理语义)。
        """
        new_settings.ensure_dirs()

        new_storage = build_storage(new_settings)
        try:
            await new_storage.connect()
            await new_storage.init_schema()
        except BaseException:
            try:
                await new_storage.close()
            except Exception:  # noqa: BLE001
                log.exception("关闭校验用的 storage 失败")
            raise
        try:
            await new_storage.close()
        except Exception as e:  # noqa: BLE001
            log.warning("关闭校验用的 storage 失败: %s", e)

        new_objects = build_object_store(new_settings)
        try:
            await new_objects.connect()
        except BaseException:
            try:
                await new_objects.close()
            except Exception:  # noqa: BLE001
                log.exception("关闭校验用的 objectstore 失败")
            raise
        try:
            await new_objects.close()
        except Exception as e:  # noqa: BLE001
            log.warning("关闭校验用的 objectstore 失败: %s", e)


def _what_label(diff: SettingsDiff) -> str:
    """`SettingsChanged.what` 字段标签(给 UI 分类显示)。

    优先级:storage+objectstore > storage > objectstore > client > credentials
    (client 单独成立时是纯 proxy / session_dir 变化,storage/objects 都没碰;
    credentials 单独成立时是纯凭据变化)。
    """
    if diff.storage_changed and diff.objects_changed:
        return "storage+objectstore"
    if diff.storage_changed:
        return "storage"
    if diff.objects_changed:
        return "objectstore"
    if diff.client_changed:
        return "client"
    return "credentials"
