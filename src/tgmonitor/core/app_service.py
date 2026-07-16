"""AppService — UI 唯一入口门面。

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
from typing import AsyncIterator

from tgmonitor.core.config import Settings
from tgmonitor.core.dto import (
    ChannelDTO,
    ExportRequest,
    MessageDTO,
    SyncOptions,
    SyncResult,
)
from tgmonitor.core.events import (
    ChannelSubscribed,
    ChannelUnsubscribed,
    ErrorOccurred,
    EventBus,
    SettingsChanged,
)
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.objectstore.factory import build_object_store
from tgmonitor.core.storage.factory import build_storage
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream

log = logging.getLogger(__name__)


class AppService:
    """UI-facing facade.所有方法都是 async,接受/返回 DTO。"""

    def __init__(
        self,
        bus: EventBus,
        client: TelegramClient,
        storage: StorageRepository,
        objects: ObjectStore,
        settings: Settings,
    ) -> None:
        self.bus = bus
        self.client = client
        self.storage = storage
        self.objects = objects
        self.settings = settings
        # 内部状态
        self._subscribed: set[int] = set()
        self._update_streams: list[UpdateStream] = []
        self._running = False
        # 重入锁:reconfigure 期间阻止 save_message
        self._reconfiguring = False
        # 全量同步服务(用户多选触发)— 延迟初始化避免循环 import
        from tgmonitor.core.channel_sync import ChannelSyncService
        self.channel_sync = ChannelSyncService(bus, client, storage)

    # ---------- 鉴权 ----------

    async def get_login_state(self) -> str:
        return self.client.state

    def _check_credentials(self) -> str | None:
        """若凭据未配置,返回错误消息(供 UI 展示);否则返回 None。"""
        s = self.settings
        if s.api_id <= 0:
            return "TG_API_ID 未配置:请打开 设置… 填写"
        if not s.api_hash or len(s.api_hash) < 16:
            return "TG_API_HASH 未配置或过短:请打开 设置… 填写"
        if not s.phone.startswith("+"):
            return "TG_PHONE 未配置(需 + 国家区号):请打开 设置… 填写"
        return None

    async def bootstrap(self) -> tuple[str, str | None]:
        """应用启动时调用一次:自动检测本地 session,有效就直接 ready,无效走 login。

        返回 (state, detail)。
        """
        # 把 storage 里 `is_subscribed=True` 的频道加载到内存 —
        # 用 `list_subscribed_channels()` 而不是 `list_channels()`:
        # - 老用户升级:旧 channels.json 没 is_subscribed 字段,InMemoryRepository
        #   和 storage 三仓实现都默认 True(保留"存即订"语义),所以效果不变。
        # - 新 sync 发现频道但用户未订阅:不进入 _subscribed,不会偷偷监听。
        persisted = await self.storage.list_subscribed_channels()
        self._subscribed = {c.id for c in persisted}

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
                self.settings, use_fake=False, event_bus=self._bus,
            )
            state, detail = await self.client.start()
        # client 端已经 publish 过 LoginStateChanged,这里只 fail-safe 再发一次终态
        if state == "error":
            await self.bus.publish(ErrorOccurred(
                source="bootstrap", message=detail or "start failed",
            ))
        return state, detail

    async def submit_phone(self, phone: str) -> tuple[str, str | None]:
        """用户点「登录」按钮 — 提交手机号 + 触发 aiotdlib 发 code。"""
        err = self._check_credentials()
        if err:
            await self.bus.publish(ErrorOccurred(source="submit_phone", message=err))
            return "error", err
        try:
            return await self.client.submit_phone(phone)
        except Exception as e:  # noqa: BLE001
            await self.bus.publish(ErrorOccurred(source="submit_phone", message=str(e), exception=e))
            return "error", str(e)

    async def submit_code(self, code: str) -> tuple[str, str | None]:
        try:
            return await self.client.submit_code(code)
        except Exception as e:  # noqa: BLE001
            await self.bus.publish(ErrorOccurred(source="submit_code", message=str(e), exception=e))
            return "error", str(e)

    async def submit_password(self, password: str) -> tuple[str, str | None]:
        try:
            return await self.client.submit_password(password)
        except Exception as e:  # noqa: BLE001
            await self.bus.publish(ErrorOccurred(source="submit_password", message=str(e), exception=e))
            return "error", str(e)

    # ---------- 频道 ----------

    async def list_joined_channels(self) -> list[ChannelDTO]:
        return await self.client.list_joined_channels()

    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        # 单一来源:storage.is_subscribed=True 的频道。
        # 不再以 `self._subscribed` in-memory set 为主 — 否则与 storage 漂移
        # 时 UI 与 monitor 不同步。
        return await self.storage.list_subscribed_channels()

    async def subscribe_channel(self, channel: ChannelDTO) -> None:
        # 先 upsert 完整信息(标题等),再设 subscribed=True —
        # 后者用 set_channel_subscribed 不会改其他字段。
        await self.storage.upsert_channel(channel)
        await self.storage.set_channel_subscribed(channel.id, True)
        self._subscribed.add(channel.id)
        await self.bus.publish(ChannelSubscribed(channel=channel))

    async def unsubscribe_channel(self, channel_id: int) -> None:
        # 退订 = 关闭订阅标志,不动元数据 / 消息。
        # 历史消息继续在 storage 里 — 用户重新订阅能看到老历史。
        # 元数据继续被 sync 刷新 — 退订后仍能反映 title/username 变化。
        try:
            await self.storage.set_channel_subscribed(channel_id, False)
        except Exception:  # noqa: BLE001
            log.exception("set_channel_subscribed(%s, False) failed", channel_id)
        self._subscribed.discard(channel_id)
        await self.bus.publish(ChannelUnsubscribed(channel_id=channel_id))

    async def sync_channels(
        self,
        channel_ids: list[int],
        options: "SyncOptions",
    ) -> "SyncResult":
        """全量同步 — UI 进度对话框经此调起。

        `options` 用 dataclass,UI 端构造(delay_ms 等覆盖 Settings 默认值)。
        """
        return await self.channel_sync.sync_channels(channel_ids, options)

    # ---------- 消息流(实时) ----------

    def subscribe_updates(self) -> UpdateStream:
        s = self.client.subscribe_updates()
        self._update_streams.append(s)
        return s

    async def start_monitor(self) -> None:
        """订阅 client 的实时更新并消费(MonitorService 的细节后续接入)。"""
        if self._running:
            return
        self._running = True

    async def stop_monitor(self) -> None:
        self._running = False
        for s in self._update_streams:
            try:
                await s.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._update_streams.clear()

    # ---------- 消息查询(供 UI 显示) ----------

    async def list_messages(
        self,
        channel_ids: list[int] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int | None = 200,
    ) -> list[MessageDTO]:
        ids = channel_ids if channel_ids is not None else list(self._subscribed)
        if not ids:
            return []
        return await self.storage.list_messages(ids, date_from, date_to, limit)

    # ---------- 导出(由 ExportService 提供实现) ----------

    async def export(self, request: ExportRequest) -> AsyncIterator[None]:
        """yield 进度心跳(让 UI 不阻塞),正常结束或抛错。"""
        from tgmonitor.core.export.service import ExportService

        svc = ExportService(self.storage, self.objects, self.bus)
        async for _ in svc.run(request):
            yield

    # ---------- 关闭 ----------

    async def shutdown(self) -> None:
        await self.stop_monitor()
        # 关 TelegramClient (停 aiotdlib 的 updates_loop + tdjson 子进程)
        try:
            await self.client.close()
        except Exception:  # noqa: BLE001
            log.exception("client.close() failed")
        await self.storage.close()
        await self.objects.close()

    # ---------- 热重载 ----------

    async def reconfigure(self, new_settings: Settings) -> None:
        """用新 settings 重建 storage / objects(不重建 TelegramClient)。

        Telegram 凭据(api_id/api_hash/phone)若变化,needs_relogin=True(UI 引导登出登入)。
        """
        # 1) 解析需重登的字段
        old = self.settings
        needs_relogin = (
            old.api_id != new_settings.api_id
            or old.api_hash != new_settings.api_hash
            or old.phone != new_settings.phone
        )

        # 2) 计算 storage/objects 是否需要重建
        storage_changed = (
            old.db_backend != new_settings.db_backend
            or old.db_dsn != new_settings.db_dsn
            or old.db_root != new_settings.db_root
        )
        objects_changed = (
            old.objectstore_backend != new_settings.objectstore_backend
            or old.objectstore_root != new_settings.objectstore_root
            or old.objectstore_endpoint != new_settings.objectstore_endpoint
            or old.objectstore_bucket != new_settings.objectstore_bucket
            or old.objectstore_access_key != new_settings.objectstore_access_key
            or old.objectstore_secret_key != new_settings.objectstore_secret_key
            or old.objectstore_region != new_settings.objectstore_region
        )

        if not (storage_changed or objects_changed or needs_relogin):
            return  # 无变化

        new_settings.ensure_dirs()

        # 3) 关旧、起新(storage 优先:它在 hot path)
        if storage_changed:
            self._reconfiguring = True
            try:
                try:
                    await self.storage.close()
                except Exception as e:  # noqa: BLE001
                    log.warning("关闭旧 storage 失败: %s", e)
                new_storage = build_storage(new_settings)
                await new_storage.connect()
                await new_storage.init_schema()
                self.storage = new_storage
                # 同步已订阅频道集合 — 用 union 而不是 intersection:
                # 用户之前的订阅应该被保留(在新存储里没有的就是缺数据,
                # 也不能默默从内存里抹掉)。
                new_db_ids = {c.id for c in await new_storage.list_channels()}
                self._subscribed = (new_db_ids | self._subscribed)
            finally:
                self._reconfiguring = False

        if objects_changed:
            try:
                await self.objects.close()
            except Exception as e:  # noqa: BLE001
                log.warning("关闭旧 objectstore 失败: %s", e)
            new_objects = build_object_store(new_settings)
            await new_objects.connect()
            self.objects = new_objects

        # 4) 提交新 settings + 事件
        self.settings = new_settings
        await self.bus.publish(
            SettingsChanged(
                what=("storage+objectstore" if storage_changed and objects_changed
                      else "storage" if storage_changed
                      else "objectstore" if objects_changed
                      else "credentials"),
                new_settings=new_settings,
                needs_relogin=needs_relogin,
            )
        )

