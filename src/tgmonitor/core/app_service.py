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

import asyncio
import dataclasses
import logging
import mimetypes
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

from tgmonitor.core.auth_service import AuthService
from tgmonitor.core.config import Settings
from tgmonitor.core.dto import (
    ChannelDTO,
    ExportRequest,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    OpenMediaResult,
    SyncOptions,
    SyncResult,
)
from tgmonitor.core.events import (
    ChannelSubscribed,
    ChannelUnsubscribed,
    ErrorOccurred,
    EventBus,
    MediaDeleted,
    MediaDownloaded,
    MediaReconcileFinished,
    MediaRetried,
    SettingsChanged,
)
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.objectstore.factory import build_object_store
from tgmonitor.core.settings_store import SettingsDiff, diff_settings
from tgmonitor.core.storage.factory import build_storage
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream

if TYPE_CHECKING:
    from tgmonitor.core.monitor.service import MonitorService

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
        monitor: MonitorService | None = None,
    ) -> None:
        """5 个子系统引用 + 内部状态。`channel_sync` 延迟 import 避免循环。

        AuthService 在此构造(2026-08-03 微切抽出),持同样的 `bus + client +
        settings` — 不需要 storage / objects。

        `monitor` 由组合根(app.py)注入(2026-08-18 修热重载):reconfigure
        要把新 storage/objects/settings 同步给 monitor,否则热重载切 PG 后
        monitor 仍写旧库,重启才生效。
        """
        self.bus = bus
        self.client = client
        self.storage = storage
        self.objects = objects
        self.settings = settings
        self.monitor = monitor
        # 内部状态
        self._update_streams: list[UpdateStream] = []
        self._running = False
        # 重入锁:reconfigure 期间阻止 save_message
        self._reconfiguring = False
        # 全量同步服务(用户多选触发)— 延迟初始化避免循环 import
        from tgmonitor.core.channel_sync import ChannelSyncService
        # 2026-08-24:与 monitor 共享同一个 MediaDownloader 实例(FULL 策略下
        # sync 也会复用做媒体下载);非 FULL 策略 / 未接线时传 None,sync 跳过下载。
        self.downloader = (
            monitor.downloader if (monitor is not None) else None
        )
        self.channel_sync = ChannelSyncService(
            bus, client, storage,
            downloader=self.downloader,
            objects=objects,
            media_policy=settings.media_policy,
        )
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

    # ---------- Media Manager(2026-08-24 新增) ----------

    async def list_media(
        self,
        *,
        channel_id: int | None = None,
        status: MediaDownloadStatus | None = None,
        media_type: MediaType | None = None,
        search: str = "",
        limit: int = 1000,
    ) -> list[tuple[MessageDTO, int, MediaDTO]]:
        """列出已下载 / 失败 / 下载中媒体(2026-08-25 PR #3 下沉)— 单行转发 storage。

        filter 全部下沉到 `StorageRepository.list_media` 后端(InMemory /
        Jsonl 顺序扫 + slice,Postgres SQL JOIN,Mongo aggregate),UI 不再
        触碰应用层 flatten。`offset` 暂不暴露给 UI(VM 一律默认 0)。
        """
        channel_ids = [channel_id] if channel_id is not None else None
        return await self.storage.list_media(
            channel_ids=channel_ids,
            status=status,
            media_type=media_type,
            search=search,
            limit=limit,
            offset=0,
        )

    async def delete_media(
        self, channel_id: int, telegram_msg_id: int, media_idx: int,
    ) -> None:
        """摘 media from message + refcount=0 时清 bytes + 发 MediaDeleted。

        与 `MonitorService.delete_message` 的 bytes 清理语义一致:跨消息去重
        场景下,另一 message 仍引用同 `object_key` 时只摘当前 message 的 media,
        不动 bytes;无引用时 `objects.delete(key)` 释放磁盘。
        """
        msg = await self.storage.get_message(channel_id, telegram_msg_id)
        if msg is None or media_idx >= len(msg.media):
            return
        med = msg.media[media_idx]
        object_key = med.object_key
        new_media = msg.media[:media_idx] + msg.media[media_idx + 1:]
        new_msg = dataclasses.replace(msg, media=new_media)
        await self.storage.update_message(new_msg)
        if object_key:
            try:
                n = await self.storage.count_media_by_object_key(object_key)
            except Exception:  # noqa: BLE001
                log.exception("count media by key failed: %s", object_key)
                n = 0
            if n == 0 and self.objects is not None:
                try:
                    await self.objects.delete(object_key)
                except Exception:  # noqa: BLE001
                    log.warning(
                        "delete bytes %s failed (already gone?)", object_key,
                        exc_info=True,
                    )
        await self.bus.publish(MediaDeleted(
            channel_id=channel_id,
            telegram_msg_id=telegram_msg_id,
            media_idx=media_idx,
        ))

    async def delete_by_channel(self, channel_id: int) -> int:
        """2026-08-25 PR #4:批量删某频道所有 message + 顺手清孤儿 bytes。

        与 `MonitorService.delete_message` 的 bytes 清理语义一致:对每条
        待删 message 的每个 `media.object_key` 做 refcount 检查,=0 时
        调 `objects.delete(key)`。

        跨频道行为:不动其它 channel 的 message / media — 用户典型诉求
        「这个频道我不想留媒体,一键清空」,只清目标频道。

        退出语义:中途 storage.delete_message 抛错 → 已删除的不回滚,
        异常上抛让调用方知道部分成功(2026-08-25:用户确认「不要回滚,
        上抛提示」即可,后续按需加 dry-run preview)。
        """
        msgs = await self.storage.list_messages([channel_id], limit=None)
        deleted = 0
        for msg in msgs:
            # 1) 先记下该 message 的所有 object_key(用于后续 refcount)
            keys: list[str] = [
                med.object_key for med in msg.media
                if med.object_key and med.download_status == MediaDownloadStatus.DONE
            ]
            try:
                await self.storage.delete_message(channel_id, msg.telegram_msg_id)
            except Exception:  # noqa: BLE001
                log.exception(
                    "delete_by_channel partial failure: channel=%s msg=%s",
                    channel_id, msg.telegram_msg_id,
                )
                continue
            deleted += 1
            # 2) 删 message 后,逐 key 检查 refcount;=0 则清 bytes
            for key in keys:
                try:
                    n = await self.storage.count_media_by_object_key(key)
                except Exception:  # noqa: BLE001
                    log.exception("count media by key failed: %s", key)
                    continue
                if n == 0 and self.objects is not None:
                    try:
                        await self.objects.delete(key)
                    except Exception:  # noqa: BLE001
                        log.warning(
                            "delete_by_channel bytes %s failed", key,
                            exc_info=True,
                        )
        log.info(
            "delete_by_channel: channel=%s deleted=%d", channel_id, deleted,
        )
        return deleted

    async def retry_media(
        self, channel_id: int, telegram_msg_id: int, media_idx: int,
    ) -> None:
        """重下 FAILED media:`objects.delete(old_key)` + download_one(force=True)。

        非 FAILED 状态直接返回(不报错,UI 通常 disable Retry 按钮,这里兜底)。
        成功后回写 storage + 发 MediaDownloaded(LIVE view 据此刷新状态)。
        """
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
        new_msg = dataclasses.replace(msg, media=tuple(new_media))
        await self.storage.update_message(new_msg)
        # 清旧 bytes — 让 download_one(force=True) 一定走真下载路径
        if old_object_key and self.objects is not None:
            try:
                await self.objects.delete(old_object_key)
            except Exception:  # noqa: BLE001
                log.warning(
                    "retry pre-clean bytes %s failed", old_object_key,
                    exc_info=True,
                )
        # 先发 MediaRetried,UI 立刻把状态切到 PENDING(避免用户重复点 Retry)
        await self.bus.publish(MediaRetried(
            channel_id=channel_id,
            telegram_msg_id=telegram_msg_id,
            media_idx=media_idx,
        ))
        # 然后走同步下载路径(retry 走 AppService 直调,不走 worker queue —
        # 2026-08-24 D4:不增加 force flag 进 queue,避免协议变更)
        if self.downloader is not None:
            try:
                updated = await self.downloader.download_one(
                    msg_pk=msg.id, media=new_med, force=True,
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
            final_msg = dataclasses.replace(new_msg, media=tuple(final_media))
            await self.storage.update_message(final_msg)
            await self.bus.publish(MediaDownloaded(
                channel_id=channel_id,
                telegram_msg_id=telegram_msg_id,
                media=updated,
            ))

    async def load_thumbnail_bytes(self, media: MediaDTO) -> bytes | None:
        """读 media 的缩略图 bytes — UI 渲染缩略图用(2026-08-25 PR #1)。

        优先 `thumb_key`(TG 端小缩略图,通常 90×90 JPEG);缺失则用
        `object_key` 原图(decoder 仍能 render)。仅 DONE + 有 objectstore 时
        才读;任何异常返 None 让 UI 保持 emoji 占位。

        设计取舍(2026-08-25 PR #1 E1):
        - 全量读 bytes 不流式 — 缩略图一般 ≤ 50KB,本地 FS / S3 都是单次
          GET;流式 (open_read iterator) 在这里收益小于代码复杂度。
        - 不调 LRU 缓存(进程内 UI 层做,service 层不持 Qt 状态)。
        - 用 `objects.open_read(key)` 而非 `get(key)` — 接口统一,Local /
          Folder 后端都用 BytesIO;失败时仍走 try/except 兜底。
        """
        if media.download_status != MediaDownloadStatus.DONE:
            return None
        if self.objects is None:
            return None
        backend = media.object_backend
        if not backend:
            return None
        # thumb 优先,缺则用原图
        key = media.thumb_key or media.object_key
        if not key:
            return None
        try:
            stream = await self.objects.open_read(key)
            try:
                data = stream.read()
            finally:
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
            return data
        except Exception:  # noqa: BLE001 — KeyError / S3 ClientError / 任何错
            log.warning(
                "load_thumbnail_bytes failed: backend=%s key=%s",
                backend, key, exc_info=True,
            )
            return None

    async def open_media(
        self, channel_id: int, telegram_msg_id: int, media_idx: int,
    ) -> bool:
        """系统默认程序打开 media 文件。True = 成功发起,False = 不可打开。

        2026-08-25 v1.3.0 PR #5:扩展到 S3 后端(走 `_stage_s3_to_tmp`)并暴露
        失败原因。返回 bool 是向后兼容 wrapper — 真实实现走 `open_media_with_result`,
        UI 失败时显示 reason。
        """
        return (
            await self.open_media_with_result(channel_id, telegram_msg_id, media_idx)
        ).success

    async def open_media_with_result(
        self, channel_id: int, telegram_msg_id: int, media_idx: int,
    ) -> OpenMediaResult:
        """打开 media + 返回结构化结果(2026-08-25 v1.3.0 PR #5)。

        Local / Folder:直接 `QDesktopServices.openUrl(QUrl.fromLocalFile(...))`。
        S3:把 ObjectStore bytes 写到 `QStandardPaths.TempLocation` 下的 tmp 文件
        再 openUrl;tmp 成功路径不主动 unlink(交给 OS 在 app exit / 重启时回收;
        Windows 上 OS 持有 handle 时 unlink 会失败,故意不冒此风险),失败路径
        显式 unlink 兜底。

        所有异常(对象存储连接断 / get 失败 / tmp 写失败 / openUrl 返 False)都
        收口到 OpenMediaResult,不让 UI 看到原始堆栈。
        """
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from tgmonitor.core.objectstore.folder_store import FolderObjectStore
        from tgmonitor.core.objectstore.local_store import LocalObjectStore
        from tgmonitor.core.objectstore.s3_store import S3ObjectStore

        msg = await self.storage.get_message(channel_id, telegram_msg_id)
        if msg is None or media_idx >= len(msg.media):
            return OpenMediaResult(False, "消息或媒体不存在")
        med = msg.media[media_idx]
        if not med.object_key or med.download_status != MediaDownloadStatus.DONE:
            return OpenMediaResult(False, "媒体未下载完成")

        try:
            if isinstance(self.objects, (LocalObjectStore, FolderObjectStore)):
                # 用 backend 自带的 _path 而非 self.objects._root / key —
                # FolderObjectStore 用 `media/<ab>/<cd>/<name>` 分片式相对路径,
                # 直接拼 root 会落到错的子目录。
                abs_path = self.objects._path(med.object_key)  # noqa: SLF001
                ok = bool(
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(abs_path)))
                )
                return (
                    OpenMediaResult(True) if ok
                    else OpenMediaResult(False, "系统调用失败:请检查是否已关联默认应用")
                )
            if isinstance(self.objects, S3ObjectStore):
                tmp = await self._stage_s3_to_tmp(med)
                ok = bool(
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(tmp)))
                )
                if not ok:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    return OpenMediaResult(
                        False, f"系统调用失败:无法打开临时文件 {tmp.name}",
                    )
                return OpenMediaResult(True)
            return OpenMediaResult(
                False,
                f"不支持的对象存储后端: {type(self.objects).__name__}",
            )
        except Exception as exc:  # noqa: BLE001 — 收口,UI 不应见堆栈
            return OpenMediaResult(False, f"{type(exc).__name__}: {exc}")

    async def _stage_s3_to_tmp(self, med: MediaDTO) -> Path:
        """2026-08-25 v1.3.0 PR #5:把 S3 media bytes 写到本地 tmp 文件用于
        `QDesktopServices.openUrl`。

        - 扩展名推断优先级:`med.file_name` 后缀 > `med.mime_type` 查
          `mimetypes.guess_extension` > `.bin` fallback
        - tmp 目录:`QStandardPaths.TempLocation`(macOS 是 per-user tmp),
          不可写时回退 `~/.cache/tgmonitor`
        - 文件名:`tgmonitor-<secrets.token_hex(8)><suffix>` — `tgmonitor-`
          前缀留给未来 sweep 工具批量清理
        - 写文件用 `asyncio.to_thread(tmp_path.write_bytes, data)` 防卡 loop
        """
        # 1. suffix
        suffix = ""
        if med.file_name:
            suffix = os.path.splitext(med.file_name)[1]
        if not suffix and med.mime_type:
            guessed = mimetypes.guess_extension(med.mime_type, strict=False)
            if guessed:
                suffix = guessed
        if not suffix:
            suffix = ".bin"

        # 2. tmp 目录
        try:
            from PySide6.QtCore import QStandardPaths

            tmp_dir_str = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.TempLocation,
            )
        except Exception:  # noqa: BLE001 — PySide6 不可用兜底
            tmp_dir_str = ""
        tmp_dir = (
            Path(tmp_dir_str) if tmp_dir_str
            else Path.home() / ".cache" / "tgmonitor"
        )
        await asyncio.to_thread(tmp_dir.mkdir, parents=True, exist_ok=True)

        # 3. 写文件
        tmp_path = tmp_dir / f"tgmonitor-{secrets.token_hex(8)}{suffix}"
        data = await self.objects.get(med.object_key or "")
        await asyncio.to_thread(tmp_path.write_bytes, data)
        return tmp_path

    async def reconcile_orphans(self, *, dry_run: bool = True) -> MediaReconcileFinished:
        """扫描 ObjectStore vs storage 媒体索引,孤儿 = ObjectStore 里有但 storage 没引用。

        dry_run=True(默认)只 log 不删;Media Manager 「Prune Orphans」按钮显式
        触发 dry_run=False 真删。S3 后端:
        - 未连接(iter_keys raise RuntimeError)→ 当作 scanned=0 兜底
        - raise NotImplementedError(理论上不再发生)→ 同上兜底
        """
        backend = self.objects.backend_name if self.objects else ""
        scanned_keys: set[str] = set()
        referenced_keys: set[str] = set()
        if self.objects is not None and hasattr(self.objects, "iter_keys"):
            try:
                async for k in self.objects.iter_keys(prefix="media/"):
                    scanned_keys.add(k)
            except (NotImplementedError, RuntimeError) as e:
                # 2026-08-25 PR #2:加 RuntimeError(S3 未连接会 raise "未连接")
                log.info(
                    "reconcile skipped: %s backend iter_keys unavailable: %s",
                    backend, e,
                )
        chs = await self.storage.list_channels()
        if chs:
            msgs = await self.storage.list_messages(
                [c.id for c in chs], limit=100_000,
            )
            for m in msgs:
                for med in m.media:
                    if (med.object_key
                            and med.download_status == MediaDownloadStatus.DONE):
                        referenced_keys.add(med.object_key)
        orphans = scanned_keys - referenced_keys
        deleted = 0
        if not dry_run and self.objects is not None and orphans:
            for k in orphans:
                try:
                    await self.objects.delete(k)
                    deleted += 1
                except Exception:  # noqa: BLE001
                    log.warning("reconcile delete %s failed", k, exc_info=True)
        evt = MediaReconcileFinished(
            backend=backend,
            scanned=len(scanned_keys),
            referenced=len(referenced_keys),
            orphans=len(orphans),
            deleted=deleted,
            dry_run=dry_run,
        )
        log.info(
            "reconcile: backend=%s scanned=%d referenced=%d orphans=%d "
            "deleted=%d dry_run=%s",
            backend, evt.scanned, evt.referenced, evt.orphans, evt.deleted,
            dry_run,
        )
        await self.bus.publish(evt)
        return evt

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
          6. publish `SettingsChanged` + 提交新 settings

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
                self.storage, self.objects, new_settings,
            )
            # 2026-08-24:reconfigure 后 monitor 重建了 MediaDownloader(新
            # storage / objects / max_bytes),同步给 self.downloader 让 sync
            # 也用新实例。policy 变化同样需要 — 直接拿新 settings 字段。
            self.downloader = self.monitor.downloader
        from tgmonitor.core.channel_sync import ChannelSyncService
        self.channel_sync = ChannelSyncService(
            self.bus, self.client, self.storage,
            downloader=self.downloader,
            objects=self.objects,
            media_policy=new_settings.media_policy,
        )

        # 6) 提交新 settings + 事件
        self.settings = new_settings
        # AuthService 持旧 settings 引用的话,_check_credentials 预检用旧凭据
        # (2026-08-18:reconfigure 后重建,消除引用不一致)
        self.auth = AuthService(self.bus, self.client, new_settings)
        await self.bus.publish(SettingsChanged(
            what=_what_label(diff),
            new_settings=new_settings,
            needs_relogin=diff.needs_relogin,
            needs_restart=diff.client_changed,  # proxy / session_dir 变更需重启生效
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

