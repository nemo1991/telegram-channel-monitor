"""MonitorService — 监听服务核心。

职责:
1. 订阅 `TelegramClient.subscribe_updates()` 的实时更新
2. 过滤(只处理用户已订阅的频道)
3. 去重(以 `(channel_id, telegram_msg_id)` 幂等 upsert)
4. 媒体 → 走 ObjectStore 入库(若策略允许)
5. 落库 + 发 `MessageReceived` 事件

启动/停止:`start()` / `stop()`,由 `AppService.start_monitor()` 调用。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Iterable

from tgmonitor.core.config import MediaPolicy, Settings
from tgmonitor.core.dto import MediaDTO, MessageDTO
from tgmonitor.core.events import (
    ErrorOccurred,
    EventBus,
    MessageDeleted,
    MessageReceived,
)
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream

log = logging.getLogger(__name__)


class MonitorService:
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
        self._task: asyncio.Task | None = None
        self._stream: UpdateStream | None = None
        self._stop = asyncio.Event()
        self._whitelist: set[int] = set()  # 被订阅的 channel_id

    def set_whitelist(self, channel_ids: Iterable[int]) -> None:
        self._whitelist = set(channel_ids)

    def add_to_whitelist(self, channel_id: int) -> None:
        self._whitelist.add(channel_id)

    def remove_from_whitelist(self, channel_id: int) -> None:
        self._whitelist.discard(channel_id)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._stream = self.client.subscribe_updates()
        self._task = asyncio.create_task(self._run(), name="MonitorService")
        log.info(
            "MonitorService started; whitelist size=%d",
            len(self._whitelist),
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._stream is not None:
            try:
                await self._stream.aclose()
            except Exception:  # noqa: BLE001
                pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    # ---- 主循环 ----

    async def _run(self) -> None:
        assert self._stream is not None
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async for msg in self._stream:
                    if self._stop.is_set():
                        break
                    try:
                        await self._handle(msg)
                    except Exception as e:  # noqa: BLE001
                        log.exception("handle message failed: %s", e)
                        await self.bus.publish(
                            ErrorOccurred(
                                source="monitor.handle",
                                message=str(e),
                                exception=e,
                            )
                        )
                # 正常退出(流关闭)
                break
            except Exception as e:  # noqa: BLE001
                log.exception("monitor loop crashed, will reconnect in %.1fs: %s", backoff, e)
                await self.bus.publish(
                    ErrorOccurred(source="monitor.loop", message=str(e), exception=e)
                )
                # 退避后重新订阅
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    break  # 停止事件触发
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)
                try:
                    if self._stream is not None:
                        await self._stream.aclose()
                except Exception:  # noqa: BLE001
                    pass
                self._stream = self.client.subscribe_updates()

    async def _handle(self, msg: MessageDTO) -> None:
        if msg.channel_id not in self._whitelist:
            return
        # 媒体下载策略(此处只做"标注"——实际下载由后台任务按需触发,见 Downloader)
        # 默认行为:无文件二进制,只存元数据 + 缩略图(若 policy 包含)
        if msg.media and self.settings.media_policy != MediaPolicy.METADATA:
            for med in msg.media:
                await self._maybe_store_thumb(med)
                if self.settings.media_policy == MediaPolicy.FULL:
                    # FULL 模式:尝试按 telegram_file_id 下载原文件(待 Downloader 接入)
                    pass

        # 幂等落库
        await self.storage.save_message(msg)
        await self.bus.publish(MessageReceived(message=msg))

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        await self.storage.delete_message(channel_id, telegram_msg_id)
        await self.bus.publish(
            MessageDeleted(channel_id=channel_id, telegram_msg_id=telegram_msg_id)
        )

    # ---- helpers ----

    async def _maybe_store_thumb(self, med: MediaDTO) -> None:
        """若 media 已带 thumb_key(由 TdlibClient 预先下载),什么都不做;
        否则若策略允许,什么都不做(留给后台 Downloader)。
        此方法只是给未来扩展点:当 media 携带缩略图 bytes 字段时,可在此入 ObjectStore。
        """
        return None


# ---------- 后台下载器(可选,生产需要时可启动) ----------

class MediaDownloader:
    """按 telegram_file_id 异步下载原文件/缩略图入 ObjectStore,然后回写 DB 的 object_key。

    简化:此处仅占位,真实实现需通过 TdlibClient.download_file(file_id, priority=1) 拿 bytes。
    """

    def __init__(self, client: TelegramClient, storage: StorageRepository, objects: ObjectStore):
        self.client = client
        self.storage = storage
        self.objects = objects

    @staticmethod
    def make_key(media: MediaDTO, suffix: str = "") -> str:
        h = hashlib.sha256((media.telegram_file_id or media.file_name or "").encode()).hexdigest()[:16]
        ext = (media.file_name or "").split(".")[-1] if media.file_name else "bin"
        return f"media/{h}.{ext}{suffix}"

    async def download_one(self, msg_pk: int, media: MediaDTO) -> str | None:
        """真实实现:`bytes = await self.client.download_file(media.telegram_file_id)`。"""
        return None
