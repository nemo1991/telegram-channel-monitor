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
from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MessageDTO
from tgmonitor.core.events import (
    ErrorOccurred,
    EventBus,
    MediaDownloaded,
    MessageDeleted,
    MessageReceived,
)
from tgmonitor.core.objectstore.base import ObjectMeta, ObjectStore
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream
from tgmonitor.core.telegram.tdlib_channels import ChatUnavailableError
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
        # 补拉中判定"频道不可访问"的 channel_id(每频道只 warning 一次,
        # 避免每 30s 轮刷日志);某频道补拉成功后从集合移除,可重新 warning。
        self._unavailable_channels: set[int] = set()
        # 异步下载队列:FULL 策略时 `_handle` 先落库(media 标 DOWNLOADING),
        # 再入队由 `_download_worker` 串行消费;下载结束后回写状态 + 发
        # MediaDownloaded 事件。串行单 worker 避免 TDLib 并发下载互相干扰。
        self._download_queue: asyncio.Queue[tuple[MessageDTO, int]] | None = None
        self._download_task: asyncio.Task | None = None

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
        # 异步下载 worker 仅在接了 MediaDownloader 时启动(FULL 策略)。
        if self.downloader is not None:
            self._download_queue = asyncio.Queue()
            self._download_task = asyncio.create_task(
                self._download_worker(), name="MonitorService.download"
            )
        log.info(
            "MonitorService started; whitelist size=%d",
            len(self._whitelist),
        )

    async def stop(self) -> None:
        """停 monitor + 关流 + 等 task 退出(2s 超时硬 cancel)。"""
        self._stop.set()
        if self._download_task is not None:
            self._download_task.cancel()
            try:
                await self._download_task
            except asyncio.CancelledError:
                pass
            self._download_task = None
        self._download_queue = None
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
        except ChatUnavailableError as e:
            # 频道不可访问(新 session 未加载 / 已被移除):只 warning 一次,
            # 不再每轮刷 error traceback;恢复可用(下一轮成功)后清掉标记。
            if channel_id not in self._unavailable_channels:
                self._unavailable_channels.add(channel_id)
                log.warning("backfill channel %d skipped: %s", channel_id, e)
        except Exception:  # noqa: BLE001
            log.exception("backfill channel %d failed", channel_id)
        else:
            # 本轮成功 → 清除不可用标记(若之前被跳过,恢复后可重新 warning)
            self._unavailable_channels.discard(channel_id)

    async def _backfill_all(self) -> None:
        """对白名单里每个频道跑一轮补拉(频控:每频道每轮一次 getChatHistory)。

        未登录(非 ready)时不拉取 —— TDLib 未就绪时 getChatHistory 会抛
        "Client not started",每轮刷错误日志;登录成功 state 变 ready 后,
        下一轮补拉自动恢复,无需外部触发。
        """
        if self.client.state != "ready":
            log.info("backfill skipped: not ready (state=%s)", self.client.state)
            return
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
        #   FULL      → thumb + 异步下载原文件(见下)
        if msg.media and self.settings.media_policy != MediaPolicy.METADATA:
            for med in msg.media:
                await self._maybe_store_thumb(med)
        # 异步下载:FULL + 已接 downloader 时,需要下载的 media 标 DOWNLOADING,
        # **先落库 + 发 MessageReceived**(用户立刻可见"下载中"),再由
        # `_download_worker` 后台下载,完成后回写状态并发 MediaDownloaded。
        # 不阻塞消息落库 —— 大文件下载(最长 30 分钟)不再制造"空窗"。
        queued: list[int] = []
        queue = self._download_queue
        if (
            msg.media
            and self.settings.media_policy == MediaPolicy.FULL
            and self.downloader is not None
            and queue is not None
        ):
            for idx, med in enumerate(msg.media):
                # PENDING=新消息;DOWNLOADING=上次运行中断遗留 → 重启后重新下载;
                # DONE / FAILED 不再重下(FAILED 让用户看原因,不无限重试)。
                if med.download_status in (
                    MediaDownloadStatus.PENDING,
                    MediaDownloadStatus.DOWNLOADING,
                ) and not med.object_key:
                    med.download_status = MediaDownloadStatus.DOWNLOADING
                    med.download_error = None
                    queued.append(idx)

        # 幂等落库(FULL 模式下 media 状态由异步下载队列写回,`save_message`
        # 是 upsert:InMemoryRepository 是 dict 覆写,jsonl / mongo / postgres
        # 各仓也按 (channel_id, telegram_msg_id) 更新)。
        await self.storage.save_message(msg)
        self._handled += 1
        log.debug(
            "monitor stored message: channel=%s msg_id=%s (handled=%d)",
            msg.channel_id, msg.telegram_msg_id, self._handled,
        )
        await self.bus.publish(MessageReceived(message=msg))

        # 下载任务入队(落库之后才入队 —— 若下载过程中退出,DB 里是
        # DOWNLOADING 状态,重启后 backfill 会重新入队下载)。
        if queued and queue is not None:
            for idx in queued:
                await queue.put((msg, idx))

    async def _download_worker(self) -> None:
        """串行消费下载队列:下载 → 回写 storage → 发 MediaDownloaded。

        `download_one` 契约不抛(失败也返回带 FAILED 状态的 MediaDTO),
        这里仍包一层防御,worker 本身永不因单条失败退出。
        """
        assert self.downloader is not None
        assert self._download_queue is not None
        while True:
            msg, idx = await self._download_queue.get()
            med = msg.media[idx]
            try:
                updated = await self.downloader.download_one(
                    msg_pk=msg.id, media=med,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 单条失败不影响 worker
                log.exception("download media failed: msg_pk=%s idx=%d", msg.id, idx)
                updated = dataclasses.replace(
                    med,
                    download_status=MediaDownloadStatus.FAILED,
                    download_error=f"下载异常: {e}",
                )
            msg.media[idx] = updated
            log.info(
                "media download done: channel=%s msg_id=%s idx=%d status=%s key=%s",
                msg.channel_id, msg.telegram_msg_id, idx,
                updated.download_status.value,
                updated.object_key or "-",
            )
            try:
                await self.storage.save_message(msg)
            except Exception as e:  # noqa: BLE001
                log.exception(
                    "update media status failed: channel=%s msg_id=%s: %s",
                    msg.channel_id, msg.telegram_msg_id, e,
                )
            await self.bus.publish(
                MediaDownloaded(
                    channel_id=msg.channel_id,
                    telegram_msg_id=msg.telegram_msg_id,
                    media=updated,
                )
            )
            self._download_queue.task_done()

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
        **更新过的** `MediaDTO`(`object_key` / `object_backend` / `file_size` 已填,
        `download_status=DONE`)
      - 失败**不抛也不返 None**,而是返回 `download_status=FAILED` +
        `download_error=<原因>` 的 MediaDTO —— 原因可落库、UI 可见(用户不再
        只能从日志猜"有记录无文件"是为什么)。

    边界(全部 → FAILED + 原因):
      - `telegram_file_id` 缺失 → "无 telegram_file_id"
      - `media.file_size > max_bytes` → "超过单文件上限"
      - `download_file` 返 None(超时 / 无数据)→ "下载失败/超时"
      - 实际下载 bytes > max_bytes(大小未知场景 hard cap)→ "实际大小超过上限"
      - `objects.put` 异常 → "对象存储写入失败: ..."
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

    async def download_one(self, msg_pk: int, media: MediaDTO) -> MediaDTO:
        """下载 → 入 ObjectStore → 返回更新后的 MediaDTO。

        成功:`object_key` / `object_backend` / `file_size` 已填,`download_status=DONE`;
        失败:`download_status=FAILED` + `download_error`(原因可持久化 / UI 展示),
        不抛异常。`msg_pk` 仅用于日志(消息主键,出问题时定位上下文)。
        """

        def failed(reason: str) -> MediaDTO:
            log.warning(
                "skip media msg_pk=%s %s: %s",
                msg_pk, media.file_name or media.telegram_file_id or media.type.value,
                reason,
            )
            return dataclasses.replace(
                media,
                download_status=MediaDownloadStatus.FAILED,
                download_error=reason,
            )

        fid = media.telegram_file_id
        if not fid:
            log.debug("skip media msg_pk=%s: no telegram_file_id", msg_pk)
            return failed("无 telegram_file_id")
        if self.max_bytes and media.file_size and media.file_size > self.max_bytes:
            return failed(
                f"文件 {media.file_size:,} 字节超过单文件上限 {self.max_bytes:,} 字节"
            )
        data = await self.client.download_file(fid)
        if data is None:
            return failed("下载超时或未返回数据")
        # hard cap for unknown-size downloads(sticker / 加密附件 / file_size 不可信场景)
        if self.max_bytes and len(data) > self.max_bytes:
            return failed(
                f"实际下载 {len(data):,} 字节超过单文件上限 {self.max_bytes:,} 字节"
            )
        key = self.make_key(media)
        meta = ObjectMeta(
            content_type=media.mime_type,
            size=len(data),
        )
        try:
            await self.objects.put(key, data, meta)
        except Exception as e:  # noqa: BLE001 — 契约:下载失败不抛,monitor 循环继续
            return failed(f"对象存储写入失败: {e}")
        # 返新 MediaDTO:保留原字段,只覆盖 object_key / object_backend / file_size
        # 与下载状态。dataclasses.replace 比 `__dict__` 解构更稳(保留 frozen /
        # __post_init__ 等),这里 MediaDTO 是普通 dataclass,replace() 同样适用。
        return dataclasses.replace(
            media,
            download_status=MediaDownloadStatus.DONE,
            download_error=None,
            object_key=key,
            object_backend=self.objects.backend_name,
            file_size=len(data),
        )
