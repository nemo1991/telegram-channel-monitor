"""MonitorService — 监听服务核心(facade)。

职责:
1. 订阅 `TelegramClient.subscribe_updates()` 的实时更新
2. 过滤(只处理用户已订阅的频道)
3. 去重(以 `(channel_id, telegram_msg_id)` 幂等 upsert)
4. 媒体 → 走 ObjectStore 入库(若策略允许)
5. 落库 + 发 `MessageReceived` 事件

启动/停止:`start()` / `stop()`,由 `AppService.start_monitor()` 调用。

2026-10-10 v1.12.x plan-batch2 目标 2:1,278 → 707 LOC facade,4 个 mixin
子模块按职责分离:
  - `whitelist.py`      — 白名单 / 后端热重载(WhitelistMixin)
  - `backfill.py`       — 周期补拉 / reconnect kick(BackfillMixin)
  - `orchestrator.py`   — start/stop/主循环/心跳(OrchestratorMixin)
  - `event_handlers.py` — 5 个 bus 订阅(EventHandlerMixin)
  - `delete.py`         — 删 + 孤儿 bytes 清理(DeleteMixin)

本文件仅保留:
  - 核心消息处理:`_handle` / `_handle_edited` / `_download_worker` /
    `_maybe_download_thumb`(热路径,3 个互相依赖,放一起不动)
  - 状态字段:`__init__` + 中间状态字段(5 个 mixin 都依赖)
  - 公共常量:`_BACKFILL_*` / `_HEARTBEAT_INTERVAL`
  - 公开 type 门面:`MonitorService` class(组合 5 mixin)

`MediaDownloader` 仍从此模块 re-export,保持向后兼容
(`from tgmonitor.core.monitor.service import MediaDownloader`)。
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import logging

from tgmonitor.core.config import MediaPolicy, Settings
from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MessageDTO
from tgmonitor.core.events import (
    EventBus,
    MediaDownloaded,
    MediaDownloadProgress,
    MessageEdited,
    MessageReceived,
)
from tgmonitor.core.monitor.backfill import BackfillMixin
from tgmonitor.core.monitor.delete import DeleteMixin
from tgmonitor.core.monitor.downloader import MediaDownloader  # noqa: F401  # re-export
from tgmonitor.core.monitor.event_handlers import EventHandlerMixin
from tgmonitor.core.monitor.orchestrator import OrchestratorMixin
from tgmonitor.core.monitor.whitelist import WhitelistMixin
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream

__all__ = ["MonitorService", "MediaDownloader"]  # MediaDownloader re-export

log = logging.getLogger(__name__)


class MonitorService(
    WhitelistMixin,
    OrchestratorMixin,
    BackfillMixin,
    EventHandlerMixin,
    DeleteMixin,
):
    """实时监听服务:订阅 TelegramClient.update 流 → 落库 + 发 MessageReceived。

    除了实时流,还会以 `_BACKFILL_INTERVAL` 周期对白名单频道做一轮补拉
    (见 `_backfill`):TDLib 的 `updateNewMessage` 只推送连接期间的新消息,
    断线 / 重启期间的消息不会重放,补拉负责兜底,避免监测不全 / 长空窗。

    类继承 5 个 mixin 顺序:
    `WhitelistMixin → OrchestratorMixin → BackfillMixin →
     EventHandlerMixin → DeleteMixin`
    — 都只 mixin methods,不重 `__init__`;`__init__` 集中在本类。
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
        # reconnect 立即 kick 补拉后,设 True 跳过紧接的周期 tick
        # (避免重复回拉 — 在 BackfillMixin._handle_connection_state 里设)。
        self._skip_next_tick = False
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

    # ---- 核心消息流(_handle / _handle_edited / _download_worker) ----
    # 注:3 个 method 互相依赖(共享 _seen_ids / 共享下载队列 / 共享 _handled),
    # 故意不放 mixin —— 拆开会让 facade 转发 3 层,反而增加阅读跳数。

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

        2026-09-01 v1.5.1 PR #B3:每条 media 构造 `progress_callback`
        闭包透传给 `client.download_file`,回调里发
        `MediaDownloadProgress` 事件;UI 订阅后实时显示下载百分比。
        """
        assert self.downloader is not None
        assert self._download_queue is not None
        while True:
            msg, idx = await self._download_queue.get()
            med = msg.media[idx]

            # ruff B023:闭包需显式绑 loop 变量(msg / idx)— 默认参数
            # 在 def 时求值,每次循环绑定到当前值。
            ch_id = msg.channel_id
            tg_msg_id = msg.telegram_msg_id

            async def _on_progress(
                downloaded: int, total: int | None, _ch=ch_id, _mid=tg_msg_id, _idx=idx
            ) -> None:
                """TDLib 进度回调 → 发 MediaDownloadProgress 事件。

                闭包捕获 `msg.channel_id / msg.telegram_msg_id / idx` —
                MediaDownloader 不需要知道 EventBus,事件协议在 dispatcher
                层完成注入(2026-09-01 PR #B3)。`_ch / _mid / _idx` 是
                显式绑定的默认参数,绕开 ruff B023。
                """
                await self.bus.publish(
                    MediaDownloadProgress(
                        channel_id=_ch,
                        telegram_msg_id=_mid,
                        media_idx=_idx,
                        downloaded=downloaded,
                        total=total,
                    )
                )

            try:
                updated = await self.downloader.download_one(
                    msg_pk=msg.id,
                    media=med,
                    progress_callback=_on_progress,
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
            # 2026-09-29:缩略图独立下载 — main 下完后再下 thumb 并落
            # thumbnails 表。主图 FAILED 也下 thumb(成功优先,UI 缩略图可
            # 显示让用户看到「文件丢了但缩略图在」的提示)。
            await self._maybe_download_thumb(msg, idx, med)
            await self.bus.publish(
                MediaDownloaded(
                    channel_id=msg.channel_id,
                    telegram_msg_id=msg.telegram_msg_id,
                    media=updated,
                )
            )
            self._download_queue.task_done()

    async def _maybe_download_thumb(self, msg: MessageDTO, idx: int, med: MediaDTO) -> None:
        """2026-09-29:缩略图独立下载辅助。无 thumbnail_telegram_file_id 跳过;
        download_thumb 失败仅 log 不影响主流程。
        """
        downloader = self.downloader
        if downloader is None:
            return
        if not med.thumbnail_telegram_file_id:
            return
        try:
            thumb = await downloader.download_thumb(
                msg_pk=(msg.channel_id, msg.telegram_msg_id),
                media=med,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "download thumb failed: channel=%s msg_id=%s idx=%d",
                msg.channel_id,
                msg.telegram_msg_id,
                idx,
            )
            return
        if thumb is None:
            return
        # 覆盖 media_idx — download_thumb 不知情,落库前必填。
        thumb = dataclasses.replace(thumb, media_idx=idx)
        try:
            await self.storage.save_thumbnail(thumb)
        except Exception:  # noqa: BLE001
            log.exception(
                "save_thumbnail failed: channel=%s msg_id=%s idx=%d",
                msg.channel_id,
                msg.telegram_msg_id,
                idx,
            )

    # ---- helpers ----

    # 2026-08-25 PR #3:删除 `_count_media_with_object_key` — 由
    # `storage.count_media_by_object_key` 替代,后端走 SQL/Mongo 索引 O(1),
    # InMemory/Jsonl 走顺序扫(与旧实现等价)。

    async def _maybe_store_thumb(self, med: MediaDTO) -> None:
        """2026-09-29:删(改 MediaDownloader.download_thumb 入独立 thumb/ + 缩略图表)。
        旧版本是空 hook(thumb bytes 永远没真下);新 thumbnail 由 MediaDownloader
        在 `_download_worker` 同步下 — 见 download_thumb。
        """
        return None
