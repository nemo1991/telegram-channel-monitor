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

from tgmonitor.core.auth_service import AuthService
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
from tgmonitor.core.settings_store import SettingsDiff, diff_settings
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
        """5 个子系统引用 + 内部状态。`channel_sync` 延迟 import 避免循环。

        AuthService 在此构造(2026-08-03 微切抽出),持同样的 `bus + client +
        settings` — 不需要 storage / objects。
        """
        self.bus = bus
        self.client = client
        self.storage = storage
        self.objects = objects
        self.settings = settings
        # 内部状态
        self._update_streams: list[UpdateStream] = []
        self._running = False
        # 重入锁:reconfigure 期间阻止 save_message
        self._reconfiguring = False
        # 全量同步服务(用户多选触发)— 延迟初始化避免循环 import
        from tgmonitor.core.channel_sync import ChannelSyncService
        self.channel_sync = ChannelSyncService(bus, client, storage)
        # 鉴权 façade(2026-08-03 微切抽出)— 3 个 submit_* 方法 + 凭据预检
        self.auth = AuthService(bus, client, settings)

    # ---------- 鉴权 ----------

    async def get_login_state(self) -> str:
        """当前登录状态机值(继承自 TelegramClient.state)。"""
        return self.client.state

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
                self.settings, use_fake=False, event_bus=self.bus,
            )
            state, detail = await self.client.start()
        # client 端已经 publish 过 LoginStateChanged,这里只 fail-safe 再发一次终态
        if state == "error":
            await self.bus.publish(ErrorOccurred(
                source="bootstrap", message=detail or "start failed",
            ))
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

    # ---------- 频道 ----------

    async def list_joined_channels(self) -> list[ChannelDTO]:
        """已加入 Telegram 频道(best-effort UX,不走 storage)。"""
        return await self.client.list_joined_channels()

    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        """已订阅频道 — **单一真理**走 storage(删 `_subscribed` cache 后)。
        # 2026-07-31 删 `self._subscribed` cache 后这是 AppService 唯一
        # 「订阅列表」读取入口,被 VM / monitor / channel_widget 复用。
        """
        return await self.storage.list_subscribed_channels()

    async def subscribe_channel(self, channel: ChannelDTO) -> None:
        """订阅一个频道 — upsert 完整元数据 + 设 subscribed=True + 发事件。

        # 先 upsert 完整信息(标题等),再设 subscribed=True —
        # 后者用 set_channel_subscribed 不会改其他字段。
        """
        await self.storage.upsert_channel(channel)
        await self.storage.set_channel_subscribed(channel.id, True)
        await self.bus.publish(ChannelSubscribed(channel=channel))

    async def unsubscribe_channel(self, channel_id: int) -> None:
        """退订 — 关订阅标志但保留历史 + 元数据。

        # 退订 = 关闭订阅标志,不动元数据 / 消息。
        # 历史消息继续在 storage 里 — 用户重新订阅能看到老历史。
        # 元数据继续被 sync 刷新 — 退订后仍能反映 title/username 变化。
        #
        # 2026-07-31 修 SUBSCRIBED_DRIFT_ANALYSIS #A:之前 storage 失败被
        # `log.exception` 吞后仍 emit `ChannelUnsubscribed`,UI 移走视觉
        # 元素,但 storage 持久化记录仍 `is_subscribed=True`,下次启动 reload
        # → 该频道被"恢复订阅",用户视角看不出退订成功未。现在让 storage
        # 异常直接 raise(不静默吞),让 VM / ChannelWidget 的 `run_coro` 走
        # 统一异常路径 → UI 看到 ErrorOccurred 而非假成功。
        """
        await self.storage.set_channel_subscribed(channel_id, False)
        await self.bus.publish(ChannelUnsubscribed(channel_id=channel_id))

    async def sync_channels(
        self,
        channel_ids: list[int],
        options: SyncOptions,
    ) -> SyncResult:
        """全量同步 — UI 进度对话框经此调起。

        `options` 用 dataclass,UI 端构造(delay_ms 等覆盖 Settings 默认值)。
        """
        return await self.channel_sync.sync_channels(channel_ids, options)

    # ---------- 消息流(实时) ----------

    def subscribe_updates(self) -> UpdateStream:
        """订阅实时更新流(转给 UI;关 app 时 stop_monitor 统一 aclose)。"""
        s = self.client.subscribe_updates()
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

    # ---------- 消息查询(供 UI 显示) ----------

    async def list_messages(
        self,
        channel_ids: list[int] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int | None = 200,
    ) -> list[MessageDTO]:
        """查消息 — `channel_ids=None` 时走 storage「已订」真理(修 #B 双真理问题)。

        # `channel_ids=None` 时从 storage 取**当前真理**(已订频道列表),
        # 不用 in-memory cache — 跟 `list_subscribed_channels()` 是同一个真理。
        # 2026-07-31 修 SUBSCRIBED_DRIFT_ANALYSIS #B。
        """
        if channel_ids is None:
            channel_ids = [
                c.id for c in await self.storage.list_subscribed_channels()
            ]
        if not channel_ids:
            return []
        return await self.storage.list_messages(
            channel_ids, date_from, date_to, limit,
        )

    # ---------- 导出(由 ExportService 提供实现) ----------

    async def export(self, request: ExportRequest) -> AsyncIterator[None]:
        """yield 进度心跳(让 UI 不阻塞),正常结束或抛错。"""
        from tgmonitor.core.export.service import ExportService

        svc = ExportService(self.storage, self.objects, self.bus)
        async for _ in svc.run(request):
            yield

    # ---------- 关闭 ----------

    async def shutdown(self) -> None:
        """app exit — 停 monitor + 关 client / storage / objects(顺序敏感)。"""
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

        Telegram 凭据(api_id/api_hash/phone)若变化,needs_relogin=True(UI 引导登出登入)。

        流程(2026-08-03 微切):
          1. `diff_settings` 算 needs_relogin / storage_changed / objects_changed
          2. 无变化 → return
          3. storage 优先(在 hot path)→ `_rebuild_storage`
          4. objects → `_rebuild_objects`
          5. publish `SettingsChanged` + 提交新 settings
        """
        diff = diff_settings(self.settings, new_settings)
        if not diff.changed:
            return  # 无变化

        new_settings.ensure_dirs()

        if diff.storage_changed:
            await self._rebuild_storage(new_settings)
        if diff.objects_changed:
            await self._rebuild_objects(new_settings)

        # 4) 提交新 settings + 事件
        self.settings = new_settings
        await self.bus.publish(SettingsChanged(
            what=_what_label(diff),
            new_settings=new_settings,
            needs_relogin=diff.needs_relogin,
        ))

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
        """切换 objectstore:先建新、成功后关旧(与 `_rebuild_storage` 同语义)。"""
        new_objects = build_object_store(new_settings)
        await new_objects.connect()
        try:
            await self.objects.close()
        except Exception as e:  # noqa: BLE001
            log.warning("关闭旧 objectstore 失败: %s", e)
        self.objects = new_objects


def _what_label(diff: SettingsDiff) -> str:
    """`SettingsChanged.what` 字段标签(给 UI 分类显示)。

    优先级:storage+objectstore > storage > objectstore > credentials
    (credentials 单独成立时是纯凭据变化,storage/objects 都没碰)。
    """
    if diff.storage_changed and diff.objects_changed:
        return "storage+objectstore"
    if diff.storage_changed:
        return "storage"
    if diff.objects_changed:
        return "objectstore"
    return "credentials"

