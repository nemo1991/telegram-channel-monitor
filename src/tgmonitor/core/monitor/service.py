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
import time
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
from tgmonitor.core.telegram.tdlib_errors import ClientClosingError

log = logging.getLogger(__name__)


class MonitorService:
    """实时监听服务:订阅 TelegramClient.update 流 → 落库 + 发 MessageReceived。

    除了实时流,还会以 `_BACKFILL_INTERVAL` 周期对白名单频道做一轮补拉
    (见 `_backfill`):TDLib 的 `updateNewMessage` 只推送连接期间的新消息,
    断线 / 重启期间的消息不会重放,补拉负责兜底,避免监测不全 / 长空窗。
    """

    # 周期补拉参数(测试可 monkeypatch 调小)
    _BACKFILL_INTERVAL = 30.0  # 补拉周期秒
    _BACKFILL_LIMIT = 100  # 每频道每轮单页条数(<=100,TDLib 上限)
    _BACKFILL_MAX_PAGE = 1000  # 每轮每频道最多处理条数(防御性上限,防无锚点拉全史)

    # 心跳参数:`_run` 主循环无论流是否有消息,每 `_HEARTBEAT_INTERVAL` 秒打一条
    # DEBUG 心跳(活跃频道也打,不是只在静默时) —— 有这条日志就证明实时通道还
    # 活着,是排查"空窗 / 一段时间不监听"的第一手信号(TG_LOG_LEVEL=DEBUG 可看)。
    _HEARTBEAT_INTERVAL = 30.0  # 心跳间隔秒(测试可 monkeypatch 调小)

    def __init__(
        self,
        bus: EventBus,
        client: TelegramClient,
        storage: StorageRepository,
        objects: ObjectStore,
        settings: Settings,
        downloader: MediaDownloader | None = None,
    ) -> None:
        """`downloader` 可选 — FULL 媒体策略时配置(FULL = 实际下原文件)。"""
        self.bus = bus
        self.client = client
        self.storage = storage
        self.objects = objects
        self.settings = settings
        self.downloader = downloader
        self._task: asyncio.Task | None = None
        self._backfill_task: asyncio.Task | None = None
        self._stream: UpdateStream | None = None
        self._stop = asyncio.Event()
        self._whitelist: set[int] = set()  # 被订阅的 channel_id
        self._handled = 0  # 累计成功落库消息数(心跳日志用)
        self._last_heartbeat = 0.0  # time.monotonic() 上次心跳时间(周期节流用)

    def set_whitelist(self, channel_ids: Iterable[int]) -> None:
        """替换白名单 — 由 AppService 启动 monitor 时调。"""
        self._whitelist = set(channel_ids)

    def add_to_whitelist(self, channel_id: int) -> None:
        """增量加一个频道到白名单(订阅后立即生效)。"""
        self._whitelist.add(channel_id)

    def remove_from_whitelist(self, channel_id: int) -> None:
        """从白名单摘掉一个频道(退订;不存在 idempotent 不抛)。"""
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
        """启动主循环;幂等(已启动时 no-op)。"""
        if self._task is not None:
            return
        self._stop.clear()
        self._stream = self.client.subscribe_updates()
        self._task = asyncio.create_task(self._run(), name="MonitorService")
        self._backfill_task = asyncio.create_task(
            self._backfill_loop(), name="MonitorService.backfill"
        )
        log.info(
            "MonitorService started; whitelist size=%d",
            len(self._whitelist),
        )

    async def stop(self) -> None:
        """停 monitor + 关流 + 等 task 退出(2s 超时硬 cancel)。"""
        self._stop.set()
        if self._backfill_task is not None:
            self._backfill_task.cancel()
            try:
                await self._backfill_task
            except asyncio.CancelledError:
                pass
            self._backfill_task = None
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
                while not self._stop.is_set():
                    try:
                        # 用 timeout 包住 anext:流静默超过 _HEARTBEAT_INTERVAL
                        # 秒时抛 TimeoutError → 打心跳后继续等。
                        async with asyncio.timeout(self._HEARTBEAT_INTERVAL):
                            msg = await anext(self._stream)
                    except StopAsyncIteration:
                        break  # 流正常结束(aclose 后 __anext__ 抛它)
                    except TimeoutError:
                        self._log_heartbeat(no_updates=True)
                        continue
                    if msg is None:
                        # 防御:个别流实现可能把关闭哨兵(None)漏出来而不是抛
                        # StopAsyncIteration — 同样视为流结束。
                        log.debug("monitor stream closed (None sentinel)")
                        break
                    # 有消息时也周期打心跳 —— 活跃频道下用户同样能确认通道活着,
                    # 而不是只见 update received 不见 heartbeat。
                    self._log_heartbeat(no_updates=False)
                    log.debug(
                        "monitor update received: channel=%s msg_id=%s",
                        msg.channel_id, msg.telegram_msg_id,
                    )
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

    def _log_heartbeat(self, *, no_updates: bool) -> None:
        """周期心跳 INFO 日志:距上次心跳 ≥ `_HEARTBEAT_INTERVAL` 才打。

        `no_updates=True` 表示该次由流静默超时触发(距上次消息已超过一个周期);
        `False` 表示流有消息、只是到点顺带汇报一次。两者都证明实时通道活着。
        INFO 级别是为了默认可见 —— 用户不需要设 `TG_LOG_LEVEL=DEBUG` 就能
        确认 monitor 是否在心跳(30s 一条不算日志噪音)。
        """
        now = time.monotonic()
        if now - self._last_heartbeat < self._HEARTBEAT_INTERVAL:
            return
        self._last_heartbeat = now
        log.info(
            "monitor heartbeat: stream alive%s (whitelist=%d, handled=%d)",
            ", no updates" if no_updates else "",
            len(self._whitelist),
            self._handled,
        )

    # ---- 周期补拉(断线 / 重启期间的兜底) ----

    async def _backfill(self, channel_id: int) -> None:
        """补拉该频道最近消息:TDLib 的 `updateNewMessage` 只在连接期间推送,
        断线 / 重启期间的新消息不会重放,这里以周期兜底,顺带覆盖"实时流静默
        死亡"的场景(此时 `request()` 仍可用,消息数据照常入库)。

        锚点 = 库里该频道最大 `telegram_msg_id`;`iter_chat_history` 向旧方向
        递减,遇到 `<= 锚点` 即说明其余已覆盖,break。首次(无历史)锚点 = 0,
        只处理最近 `_BACKFILL_MAX_PAGE` 条当启动预热 — **不**让无锚点频道把
        整段历史翻完(每 30s 拉几万条会撞 flood wait,反而制造长空窗)。
        """
        try:
            max_id = await self.storage.get_max_telegram_msg_id(channel_id) or 0
            n = 0
            async for msg in self.client.iter_chat_history(
                channel_id, limit=self._BACKFILL_LIMIT,
            ):
                if n >= self._BACKFILL_MAX_PAGE:
                    log.warning(
                        "backfill channel %d hit cap %d; 新消息过多,建议手动全量同步",
                        channel_id, self._BACKFILL_MAX_PAGE,
                    )
                    break
                if max_id > 0 and msg.telegram_msg_id <= max_id:
                    break
                await self._handle(msg)
                n += 1
        except ClientClosingError:
            return  # close() 中,静默退出,不打 traceback
        except Exception:  # noqa: BLE001
            log.exception("backfill channel %d failed", channel_id)

    async def _backfill_all(self) -> None:
        """对白名单里每个频道跑一轮补拉(频控:每频道每轮一次 getChatHistory)。"""
        for cid in list(self._whitelist):
            await self._backfill(cid)

    async def _backfill_loop(self) -> None:
        """周期补拉主循环:每 `_BACKFILL_INTERVAL` 秒全量扫一轮白名单。"""
        while not self._stop.is_set():
            await asyncio.sleep(self._BACKFILL_INTERVAL)
            if self._stop.is_set():
                break
            # 心跳:每轮补拉打一条 INFO —— 实时流挂了但补拉还活着时,
            # 日志里能看到两者不同步,定位"空窗"来源(INFO 默认可见)
            log.info(
                "backfill round start: whitelist=%d handled=%d",
                len(self._whitelist), self._handled,
            )
            await self._backfill_all()

    async def _handle(self, msg: MessageDTO) -> None:
        if msg.channel_id not in self._whitelist:
            log.debug(
                "monitor ignored msg: channel_id=%s not in whitelist %s",
                msg.channel_id, sorted(self._whitelist),
            )
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
        self._handled += 1
        log.debug(
            "monitor stored message: channel=%s msg_id=%s (handled=%d)",
            msg.channel_id, msg.telegram_msg_id, self._handled,
        )
        await self.bus.publish(MessageReceived(message=msg))

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """删单条消息 + 发 MessageDeleted 事件(由 UI 删除按钮 / 撤回时调)。"""
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
        """`max_bytes` = 单文件硬上限(0 = 无限制,默认 200 MB)。"""
        self.client = client
        self.storage = storage
        self.objects = objects
        self.max_bytes = max_bytes

    @staticmethod
    def make_key(media: MediaDTO, suffix: str = "") -> str:
        """生成稳定的对象 key:`media/<sha256[:16]>.<ext><suffix>`(内容寻址)。"""
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
