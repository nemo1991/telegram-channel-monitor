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
import dataclasses
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
from tgmonitor.core.objectstore.base import ObjectMeta, ObjectStore
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
        downloader: MediaDownloader | None = None,
    ) -> None:
        self.bus = bus
        self.client = client
        self.storage = storage
        self.objects = objects
        self.settings = settings
        self.downloader = downloader
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

    @property
    def subscribed_ids(self) -> frozenset[int]:
        """订阅频道 id 的快照集合 — UI 只读访问用。

        内部 `_whitelist` 仍由 `set_whitelist` / `add_to_whitelist` /
        `remove_from_whitelist` 维护;每次访问返回一个新 `frozenset` 副本,
        UI 端不会意外修改内部状态。
        """
        return frozenset(self._whitelist)

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
        # 媒体下载策略:
        #   METADATA  → 跳过 thumb / full
        #   THUMBNAIL → 走 _maybe_store_thumb(空 hook,留给未来)
        #   FULL      → thumb + MediaDownloader.download_one(若已配)
        if msg.media and self.settings.media_policy != MediaPolicy.METADATA:
            updated_media: list[MediaDTO] = []
            for med in msg.media:
                await self._maybe_store_thumb(med)
                if (
                    self.settings.media_policy == MediaPolicy.FULL
                    and self.downloader is not None
                    and not med.object_key
                ):
                    updated = await self.downloader.download_one(
                        msg_pk=msg.id, media=med,
                    )
                    if updated is not None:
                        updated_media.append(updated)
                        continue
                updated_media.append(med)
            msg.media = updated_media

        # 幂等落库(FULL 模式下 msg.media[*].object_key 已被 MediaDownloader
        # 写回,save_message 一次写入完整状态;InMemoryRepository 是 dict 覆写,
        # jsonl / mongo / postgres 各仓也按 (channel_id, telegram_msg_id) upsert)
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
    """按 telegram_file_id 异步下载原文件入 ObjectStore,然后回写 DB 的 object_key。

    REVIEW M2.1 真实现:之前 `download_one` 返回 None,FULL 模式下用户**下不到任何
    原文件**,只是元数据 + 缩略图 + 一个空 key。现在:
      - 入参:`media: MediaDTO`(含 `telegram_file_id` / `file_size` / `mime_type`)
      - 行为:`client.download_file(file_id)` 拿 bytes → `objects.put` → 返
        **更新过的** `MediaDTO`(`object_key` / `object_backend` / `file_size` 已填)
      - 失败返 None,不抛 — monitor 循环继续。

    边界:
      - `telegram_file_id` 缺失 → 返 None + DEBUG
      - `media.file_size > max_bytes` → 返 None + WARNING(0 = 无限制)
      - `download_file` 返 None → 返 None + WARNING
      - 实际下载 bytes > max_bytes(大小未知场景 hard cap)→ 返 None + WARNING
    """

    def __init__(
        self,
        client: TelegramClient,
        storage: StorageRepository,
        objects: ObjectStore,
        *,
        max_bytes: int = 200_000_000,
    ) -> None:
        self.client = client
        self.storage = storage
        self.objects = objects
        self.max_bytes = max_bytes

    @staticmethod
    def make_key(media: MediaDTO, suffix: str = "") -> str:
        h = hashlib.sha256((media.telegram_file_id or media.file_name or "").encode()).hexdigest()[:16]
        ext = (media.file_name or "").split(".")[-1] if media.file_name else "bin"
        return f"media/{h}.{ext}{suffix}"

    async def download_one(self, msg_pk: int, media: MediaDTO) -> MediaDTO | None:
        """下载 → 入 ObjectStore → 返回填了 `object_key` 的新 MediaDTO。

        `msg_pk` 仅用于日志(消息主键,出问题时定位上下文);不写 DB(写 DB 是
        `MonitorService._handle` 的责任)。
        """
        fid = media.telegram_file_id
        if not fid:
            log.debug("skip media msg_pk=%s: no telegram_file_id", msg_pk)
            return None
        if self.max_bytes and media.file_size and media.file_size > self.max_bytes:
            log.warning(
                "skip media msg_pk=%s %s: %d bytes > max %d",
                msg_pk, media.file_name or fid, media.file_size, self.max_bytes,
            )
            return None
        data = await self.client.download_file(fid)
        if data is None:
            log.warning(
                "download_file(msg_pk=%s, fid=%s) returned None", msg_pk, fid,
            )
            return None
        # hard cap for unknown-size downloads(sticker / 加密附件 / file_size 不可信场景)
        if self.max_bytes and len(data) > self.max_bytes:
            log.warning(
                "downloaded msg_pk=%s fid=%s exceeded %d bytes, dropping",
                msg_pk, fid, self.max_bytes,
            )
            return None
        key = self.make_key(media)
        meta = ObjectMeta(
            content_type=media.mime_type,
            size=len(data),
        )
        await self.objects.put(key, data, meta)
        # 返新 MediaDTO:保留原字段,只覆盖 object_key / object_backend / file_size。
        # dataclasses.replace 比 `__dict__` 解构更稳(保留 frozen / __post_init__ 等),
        # 这里 MediaDTO 是普通 dataclass,replace() 同样适用。
        return dataclasses.replace(
            media,
            object_key=key,
            object_backend=self.objects.backend_name,
            file_size=len(data),
        )
