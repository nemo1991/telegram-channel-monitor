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
import collections
import dataclasses
import hashlib
import logging
import time
from typing import Iterable

from tgmonitor.core.config import MediaPolicy, Settings
from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MessageDTO
from tgmonitor.core.events import (
    ChannelMetadataChanged,
    ConnectionStateChanged,
    ErrorOccurred,
    EventBus,
    MediaDownloaded,
    MessageDeleted,
    MessageEdited,
    MessageInteractionsChanged,
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
        # 2026-08-24:最近 stream 见过 / 落库过的 (channel_id, telegram_msg_id)。
        # 用于区分 updateNewMessage 与 updateMessageContent:命中走 _handle_edited,
        # miss 走 _handle。OrderedDict 限长 10000,超长 evict 最旧。
        self._seen_ids: collections.OrderedDict[tuple[int, int], None] = collections.OrderedDict()

    def set_whitelist(self, channel_ids: Iterable[int]) -> None:
        """替换白名单 — 由 AppService 启动 monitor 时调。"""
        self._whitelist = set(channel_ids)

    def add_to_whitelist(self, channel_id: int) -> None:
        """增量加一个频道到白名单(订阅后立即生效)。"""
        self._whitelist.add(channel_id)

    def remove_from_whitelist(self, channel_id: int) -> None:
        """从白名单摘掉一个频道(退订;不存在 idempotent 不抛)。"""
        self._whitelist.discard(channel_id)

    async def update_backends(
        self,
        storage: StorageRepository,
        objects: ObjectStore,
        settings: Settings,
    ) -> None:
        """热重载后把新 storage/objects/settings 同步给 monitor 及其下载器。

        由 `AppService.reconfigure` 调用(2026-08-18 修):之前 reconfigure
        只换 AppService 自己的引用,monitor / MediaDownloader 仍持旧
        storage —— 热重载切 PG 后实时 / 补拉消息仍写旧库,重启才生效。
        这里同步替换引用 + 重建 MediaDownloader(新 storage/objects/
        max_bytes)+ 从新 storage 重载白名单(订阅真理随存储切换)。
        """
        self.storage = storage
        self.objects = objects
        self.settings = settings
        if self.downloader is not None:
            self.downloader = MediaDownloader(
                self.client,
                storage,
                objects,
                max_bytes=settings.media_max_bytes,
            )
        # 订阅真理随存储切换:白名单从新 storage 重载;失败只降级记日志,
        # 不阻断 reconfigure 的其余提交(保留原白名单,下一次订阅/退订事件会修正)。
        try:
            subscribed = await storage.list_subscribed_channels()
        except Exception:  # noqa: BLE001
            log.exception("update_backends: 从新 storage 重载白名单失败,保持原白名单")
            return
        self.set_whitelist(c.id for c in subscribed)

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
        self._seen_ids.clear()
        self._stream = self.client.subscribe_updates()
        self._task = asyncio.create_task(self._run(), name="MonitorService")
        self._backfill_task = asyncio.create_task(
            self._backfill_loop(), name="MonitorService.backfill"
        )
        # 2026-08-27 v1.4.0 PR #10:订阅 reactions / views 增量更新事件。
        # TDLib 高频推 updateMessageInteractionInfo,落库走 bus → 单点 +
        # 订阅者异常被吞,比 stream 更安全。
        self.bus.subscribe(MessageInteractionsChanged, self._handle_interactions_changed)
        # 2026-08-27 v1.4.0 PR #11:订阅 TG 端消息删除事件。落库删 row +
        # 减 object_key refcount,与 `delete_message` 路径同语义。
        self.bus.subscribe(MessageDeleted, self._handle_message_deleted)
        # 2026-08-27 v1.4.0 PR #14:订阅频道元数据变更事件。`updateChannel`
        # 直接落库;`updateSupergroup` 需要先查 channel_id(用户名匹配)。
        self.bus.subscribe(ChannelMetadataChanged, self._handle_channel_metadata)
        self.bus.subscribe(ConnectionStateChanged, self._handle_connection_state)
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
                        msg.channel_id,
                        msg.telegram_msg_id,
                    )
                    try:
                        # 2026-08-24:按 _seen_ids 区分「新消息」与「编辑」—
                        # TDLib 编辑( updateMessageContent)走同一个 stream,
                        # 用 cache 命中标记。
                        key = (msg.channel_id, msg.telegram_msg_id)
                        if key in self._seen_ids:
                            await self._handle_edited(msg)
                        else:
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
                channel_id,
                limit=self._BACKFILL_LIMIT,
            ):
                if n >= self._BACKFILL_MAX_PAGE:
                    log.warning(
                        "backfill channel %d hit cap %d; 新消息过多,建议手动全量同步",
                        channel_id,
                        self._BACKFILL_MAX_PAGE,
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
                len(self._whitelist),
                self._handled,
            )
            await self._backfill_all()

    async def _handle(self, msg: MessageDTO) -> None:
        """新消息路径(2026-08-24):已存/已编辑的消息在 `_run` 处已路由到 `_handle_edited`。

        这里:
          - 白名单守门
          - 跨消息 media 去重(同 file_id 已有 DONE → 拷字段,不入下载队列)
          - 记录 `_seen_ids`(让后续 updateMessageContent 走编辑路径)
          - 落库 + 发 MessageReceived
        """
        if msg.channel_id not in self._whitelist:
            log.debug(
                "monitor ignored msg: channel_id=%s not in whitelist %s",
                msg.channel_id,
                sorted(self._whitelist),
            )
            return

        # 跨消息 media 去重(2026-08-24):任一先前已 DONE 的同 file_id media →
        # 拷 object_key / object_backend / file_size,后续 PENDING/DOWNLOADING +
        # `not object_key` 规则看到 object_key 已填自然不入下载队列。
        if msg.media:
            for med in msg.media:
                if med.telegram_file_id and not med.object_key:
                    prior = await self.storage.find_media_by_file_id(
                        med.telegram_file_id,
                    )
                    if (
                        prior is not None
                        and prior.download_status == MediaDownloadStatus.DONE
                        and prior.object_key
                    ):
                        med.object_key = prior.object_key
                        med.object_backend = prior.object_backend
                        med.file_size = prior.file_size
                        med.download_status = MediaDownloadStatus.DONE
                        med.download_error = None

        # 记录这条消息已见,后续 updateMessageContent 会走 _handle_edited
        key = (msg.channel_id, msg.telegram_msg_id)
        self._seen_ids[key] = None
        if len(self._seen_ids) > 10000:
            self._seen_ids.popitem(last=False)

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
                # 注:跨消息去重(D8 上面那段)已把已下载的 media 标 DONE +
                # object_key 已填,这里 `not object_key` 自然跳过。
                if (
                    med.download_status
                    in (
                        MediaDownloadStatus.PENDING,
                        MediaDownloadStatus.DOWNLOADING,
                    )
                    and not med.object_key
                ):
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
            msg.channel_id,
            msg.telegram_msg_id,
            self._handled,
        )
        await self.bus.publish(MessageReceived(message=msg))

        # 下载任务入队(落库之后才入队 —— 若下载过程中退出,DB 里是
        # DOWNLOADING 状态,重启后 backfill 会重新入队下载)。
        if queued and queue is not None:
            for idx in queued:
                await queue.put((msg, idx))

    async def _handle_edited(self, msg: MessageDTO) -> None:
        """编辑路径(2026-08-24):`_run` 路由命中 `_seen_ids` 后调这里。

        编辑的常见字段:text / views / forwards / edited 标志 / media 列表 —
        覆盖式写回 storage(`update_message`)并发 `MessageEdited` 事件。

        不入队下载(编辑不该重下媒体);不发 `MessageReceived`(避免
        MessageView 把它当新消息插入)。

        不依赖 whitelist:编辑事件来自「已订阅时收到的消息历史」,用户可能后续
        退订了频道 — 编辑仍要落库,UI 显示该消息时按 channels_changed 自行
        过滤。
        """
        existed = await self.storage.get_message(
            msg.channel_id,
            msg.telegram_msg_id,
        )
        if existed is None:
            # 编辑前消息不在 storage(罕见:用户从未订阅该频道,或 sync 还没拉过)
            # — 当作新增走 save_message。
            await self.storage.save_message(msg)
            # 注意:仍要记 _seen_ids,后续真编辑可继续走 _handle_edited 覆盖。
            self._seen_ids[(msg.channel_id, msg.telegram_msg_id)] = None
            if len(self._seen_ids) > 10000:
                self._seen_ids.popitem(last=False)
            log.debug(
                "monitor edited (no prior): channel=%s msg_id=%s",
                msg.channel_id,
                msg.telegram_msg_id,
            )
            await self.bus.publish(MessageEdited(message=msg))
            return
        # 字段级覆盖 — 保留 storage 的 message.id 与其它未列字段
        updated = dataclasses.replace(
            existed,
            text=msg.text,
            views=msg.views,
            forwards=msg.forwards,
            edited=msg.edited,
            media=msg.media,
        )
        await self.storage.update_message(updated)
        log.debug(
            "monitor edited: channel=%s msg_id=%s",
            msg.channel_id,
            msg.telegram_msg_id,
        )
        await self.bus.publish(MessageEdited(message=updated))

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
                    msg_pk=msg.id,
                    media=med,
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
                msg.channel_id,
                msg.telegram_msg_id,
                idx,
                updated.download_status.value,
                updated.object_key or "-",
            )
            try:
                await self.storage.save_message(msg)
            except Exception as e:  # noqa: BLE001
                log.exception(
                    "update media status failed: channel=%s msg_id=%s: %s",
                    msg.channel_id,
                    msg.telegram_msg_id,
                    e,
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
        """删单条消息 + 清孤儿 bytes + 发 MessageDeleted 事件。

        2026-08-24:顺手清 bytes — 对该 message 的每个 `media.object_key`
        做一次 refcount 检查(`_count_media_with_object_key`),refcount=0 时
        才 `objects.delete(key)`,避免误删跨消息去重场景下其它 message 还在
        用的 bytes。清理失败只 log,不阻断删除。

        2026-08-27 PR #11:走 `_delete_with_orphan_check` 复用;publish 仍在
        这里(AppService 用户触发的删除需要通知 UI)。
        """
        await self._delete_with_orphan_check(channel_id, telegram_msg_id)
        await self.bus.publish(
            MessageDeleted(channel_id=channel_id, telegram_msg_id=telegram_msg_id)
        )

    async def _handle_message_deleted(self, event: MessageDeleted) -> None:
        """2026-08-27 v1.4.0 PR #11:TDLib `updateDeleteMessages` → 落库删 row +
        清孤儿 bytes。**不**再 publish(避免与发布者形成无限循环)。
        """
        try:
            await self._delete_with_orphan_check(event.channel_id, event.telegram_msg_id)
        except Exception as e:  # noqa: BLE001
            log.exception("handle MessageDeleted failed: %s", e)
            await self.bus.publish(
                ErrorOccurred(
                    source="monitor.delete",
                    message=str(e),
                    exception=e,
                )
            )

    async def _delete_with_orphan_check(
        self,
        channel_id: int,
        telegram_msg_id: int,
    ) -> None:
        """PR #11:删 row + 清孤儿 bytes 核心逻辑,与 AppService.delete_message 共享。"""
        old = await self.storage.get_message(channel_id, telegram_msg_id)
        await self.storage.delete_message(channel_id, telegram_msg_id)
        if not old:
            return
        for med in old.media:
            key = med.object_key
            if not key:
                continue
            try:
                # 2026-08-25 PR #3:refcount 下沉到 storage 后端
                # (SQL count / Mongo aggregate / InMemory / Jsonl 顺序扫)。
                n = await self.storage.count_media_by_object_key(key)
            except Exception:  # noqa: BLE001
                log.exception("count media by key failed: %s", key)
                continue
            if n == 0 and self.objects is not None:
                try:
                    await self.objects.delete(key)
                    log.info(
                        "monitor delete orphan bytes: channel=%s msg=%s key=%s",
                        channel_id,
                        telegram_msg_id,
                        key,
                    )
                except Exception:  # noqa: BLE001
                    log.warning(
                        "monitor delete bytes %s failed (already gone?)",
                        key,
                        exc_info=True,
                    )

    async def _handle_interactions_changed(
        self,
        event: MessageInteractionsChanged,
    ) -> None:
        """2026-08-27 v1.4.0 PR #10:TDLib `updateMessageInteractionInfo`
        → storage `update_message_interactions`(views / reactions 增量)。

        落库后**不再 republish**(避免无限循环:EventBus.subscribe 自己是
        反模式);详情面板直接订阅 `MessageInteractionsChanged` 即可,
        落库前/落库后两条事件同 payload,UI 自取所需时点。
        """
        try:
            await self.storage.update_message_interactions(
                event.channel_id,
                event.telegram_msg_id,
                views=event.views,
                # event.reactions 已是 list[ReactionDTO](TDLib 映射层保证),
                # storage 实现负责 list/dict 双形态容错
                reactions=event.reactions,
            )
            log.debug(
                "interactions updated: channel=%s msg=%s views=%s reactions=%d",
                event.channel_id,
                event.telegram_msg_id,
                event.views,
                len(event.reactions) if event.reactions else 0,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("update interactions failed: %s", e)
            await self.bus.publish(
                ErrorOccurred(
                    source="monitor.interactions",
                    message=str(e),
                    exception=e,
                )
            )

    async def _handle_channel_metadata(
        self,
        event: ChannelMetadataChanged,
    ) -> None:
        """2026-08-27 v1.4.0 PR #14:频道元数据增量更新落库。

        `updateChannel` 推时 `channel_id` 已就绪,直接落库;`updateSupergroup`
        推时只有 `supergroup_id`,需通过 username 反查 channel_id
        (storage 现存 — InMemory/Jsonl/Postgres/Mongo 都按 username 找)。
        """
        try:
            if event.channel_id == 0 and event.supergroup_id is not None:
                # supergroup_id → username(由 storage 反查)。无图源,
                # 实际生效路径:username 相同 → 找到 channel → 落库。
                # 这里走简单策略:用 storage.list_channels() 扫描找匹配的
                # username。生产可加索引;MVP 接受 O(N) 扫。
                # v1.4.0 MVP:仅 username 不为 None 时反查。
                if event.username:
                    for c in await self.storage.list_channels():
                        if c.username == event.username:
                            await self.storage.update_channel_metadata(
                                c.id,
                                title=event.title,
                                username=event.username,
                                member_count=event.member_count,
                            )
                            log.debug(
                                "channel metadata updated via supergroup: "
                                "channel=%s member_count=%s",
                                c.id,
                                event.member_count,
                            )
                            return
                # 没找到匹配 username → 静默(可能 TDLib 推了陌生 supergroup)
                return
            if event.channel_id == 0:
                return  # 无 channel_id 又无 username,跳过
            await self.storage.update_channel_metadata(
                event.channel_id,
                title=event.title,
                username=event.username,
                member_count=event.member_count,
            )
            log.debug(
                "channel metadata updated: channel=%s title=%r username=%r member_count=%s",
                event.channel_id,
                event.title,
                event.username,
                event.member_count,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("handle ChannelMetadataChanged failed: %s", e)
            await self.bus.publish(
                ErrorOccurred(
                    source="monitor.channel_metadata",
                    message=str(e),
                    exception=e,
                )
            )

    async def _handle_connection_state(
        self,
        event: ConnectionStateChanged,
    ) -> None:
        """2026-08-27 v1.4.0 PR #14:reconnect → 立即触发 backfill,不等 30s tick。

        state=`ready` 时 kick `_backfill_all_subscribed`,并设 `_skip_next_tick`
        避免 tick 紧接着又跑一次造成回拉重复。
        """
        if event.state != "ready":
            return
        try:
            log.info("connection ready — kicking immediate backfill")
            self._skip_next_tick = True
            await self._backfill_all()
        except Exception as e:  # noqa: BLE001
            log.exception("immediate backfill on reconnect failed: %s", e)
            await self.bus.publish(
                ErrorOccurred(
                    source="monitor.backfill_kick",
                    message=str(e),
                    exception=e,
                )
            )

    # ---- helpers ----

    # 2026-08-25 PR #3:删除 `_count_media_with_object_key` — 由
    # `storage.count_media_by_object_key` 替代,后端走 SQL/Mongo 索引 O(1),
    # InMemory/Jsonl 走顺序扫(与旧实现等价)。

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
        h = hashlib.sha256((media.telegram_file_id or media.file_name or "").encode()).hexdigest()[
            :16
        ]
        ext = (media.file_name or "").split(".")[-1] if media.file_name else "bin"
        return f"media/{h}.{ext}{suffix}"

    async def download_one(
        self,
        msg_pk: int,
        media: MediaDTO,
        *,
        force: bool = False,
    ) -> MediaDTO:
        """下载 → 入 ObjectStore → 返回更新后的 MediaDTO。

        skip-if-stored 顺序(2026-08-24):
          1) storage 已有同 `telegram_file_id` 的 DONE media → 拷字段,不发 TDLib 请求
          2) ObjectStore 已有同 `make_key(media)` → 视为已下载,补 object_key / backend
          3) 两个都 miss → 走原下载流程

        `force=True` 时跳过 #1 + #2(2026-08-24 Media Manager retry 路径用):用户
        显式要求「重新尝试」,即使已下载也再发 TDLib 请求覆盖。调用方有责任
        先 `objects.delete(old_key)` 清理旧 bytes,否则写入会覆盖。

        成功:`object_key` / `object_backend` / `file_size` 已填,`download_status=DONE`;
        失败:`download_status=FAILED` + `download_error`(原因可持久化 / UI 展示),
        不抛异常。`msg_pk` 仅用于日志(消息主键,出问题时定位上下文)。
        """

        def failed(reason: str) -> MediaDTO:
            log.warning(
                "skip media msg_pk=%s %s: %s",
                msg_pk,
                media.file_name or media.telegram_file_id or media.type.value,
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

        # 跳过 #1:storage 已有同 file_id 的成功下载 → 拷字段,不发 TDLib 请求
        # `force=True`(retry)时跳过 — 用户显式要求重试,即使 prior 已存也走原下载。
        if not force:
            prior = await self.storage.find_media_by_file_id(fid)
            if (
                prior is not None
                and prior.download_status == MediaDownloadStatus.DONE
                and prior.object_key
            ):
                log.debug(
                    "skip media msg_pk=%s: storage hit (fid=%s key=%s)",
                    msg_pk,
                    fid,
                    prior.object_key,
                )
                return dataclasses.replace(
                    media,
                    download_status=MediaDownloadStatus.DONE,
                    download_error=None,
                    object_key=prior.object_key,
                    object_backend=prior.object_backend,
                    file_size=prior.file_size,
                )

        # 跳过 #2:ObjectStore 已有该 key → 视为已下载,补字段
        # `force=True` 同样跳过。
        key = self.make_key(media)
        if not force and await self.objects.exists(key):
            log.debug(
                "skip media msg_pk=%s: objectstore hit (key=%s)",
                msg_pk,
                key,
            )
            return dataclasses.replace(
                media,
                download_status=MediaDownloadStatus.DONE,
                download_error=None,
                object_key=key,
                object_backend=self.objects.backend_name,
                file_size=media.file_size,
            )

        if self.max_bytes and media.file_size and media.file_size > self.max_bytes:
            return failed(f"文件 {media.file_size:,} 字节超过单文件上限 {self.max_bytes:,} 字节")
        data = await self.client.download_file(fid)
        if data is None:
            return failed("下载超时或未返回数据")
        # hard cap for unknown-size downloads(sticker / 加密附件 / file_size 不可信场景)
        if self.max_bytes and len(data) > self.max_bytes:
            return failed(f"实际下载 {len(data):,} 字节超过单文件上限 {self.max_bytes:,} 字节")
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
