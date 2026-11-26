"""MonitorService — 监听服务核心(组合根 + 热路径)。

职责:
1. 订阅 `TelegramClient.subscribe_updates()` 的实时更新
2. 过滤(只处理用户已订阅的频道)
3. 去重(以 `(channel_id, telegram_msg_id)` 幂等 upsert)
4. 媒体 → 走 ObjectStore 入库(若策略允许)
5. 落库 + 发 `MessageReceived` 事件

启动/停止:`start()` / `stop()`,由 `AppService.start_monitor()` 调用。

2026-11-11 v2.0.0 Phase 3-C3:5 个 mixin 改为显式组合组件。
2026-11-25 P2-M4:组件改为只持共享 `MonitorRuntime`,不再反向持有宿主
(原 78 处 `self._svc.*` 归零),打破双向耦合:
  - `whitelist.py`      — 白名单 / 后端热重载(WhitelistManager)
  - `orchestrator.py`   — start/stop/主循环/心跳(Orchestrator)
  - `backfill.py`       — 周期补拉 / reconnect kick(BackfillRunner)
  - `event_handlers.py` — 5 个 bus 订阅(EventHandlers)
  - `delete.py`         — 删 + 孤儿 bytes 清理(DeleteManager)

依赖 + 可变状态单一所有权在 `MonitorRuntime`(`self._rt`);本类:
  - 核心消息处理:`_handle` / `_handle_edited` / `_download_worker` /
    `_maybe_download_thumb`(热路径,互相依赖,放一起不动)
  - 状态 / 依赖 property 转发到 `self._rt`(`monitor.storage` /
    `monitor._backfill_all` 等公开面与测试 seam 位置完全不变)
  - 跨组件晚绑定回调注入 `_rt.handle_*` 等槽(测试 patch 宿主方法仍生效)

`MediaDownloader` 仍从此模块 re-export,保持向后兼容
(`from tgmonitor.core.monitor.service import MediaDownloader`)。
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import logging
from typing import Iterable

from tgmonitor.core.config import MediaPolicy, Settings
from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MessageDTO
from tgmonitor.core.events import (
    ChannelMetadataChanged,
    ChannelPhotoChanged,
    ChannelTitleChanged,
    ConnectionStateChanged,
    EventBus,
    MediaDownloaded,
    MediaDownloadProgress,
    MessageDeleted,
    MessageEdited,
    MessageInteractionsChanged,
    MessagePinChanged,
    MessageReceived,
)
from tgmonitor.core.monitor._runtime import MonitorRuntime
from tgmonitor.core.monitor.backfill import BackfillRunner
from tgmonitor.core.monitor.delete import DeleteManager
from tgmonitor.core.monitor.downloader import MediaDownloader  # re-export
from tgmonitor.core.monitor.event_handlers import EventHandlers
from tgmonitor.core.monitor.orchestrator import Orchestrator
from tgmonitor.core.monitor.whitelist import WhitelistManager
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream

__all__ = ["MonitorService", "MediaDownloader"]  # MediaDownloader re-export

log = logging.getLogger(__name__)

# 配置默认值(P2-M4:原类常量移到模块级,实例值由 MonitorRuntime 持有并通过
# property 暴露,保留 `monitor._BACKFILL_*` / `_HEARTBEAT_INTERVAL` seam)。
_DEFAULT_BACKFILL_INTERVAL = 30.0
_DEFAULT_BACKFILL_LIMIT = 100
_DEFAULT_BACKFILL_MAX_PAGE = 1000
_DEFAULT_HEARTBEAT_INTERVAL = 30.0


def _merge_media_on_edit(old_media: list[MediaDTO], new_media: list[MediaDTO]) -> list[MediaDTO]:
    """编辑路径 media 合并(2026-11-25 P1-1 修复)。

    背景:`_handle_edited` 此前用 `media=msg.media` 整列表替换 —
    `msg.media` 是从 TG 重新映射的 DTO,`object_key` / `download_status`
    全为 None,`update_message` 走 `save_message` 的「先清后插」后,
    已下载媒体的下载状态被抹掉;`reconcile_orphans` 会把这些失去引用的
    对象判孤儿**真删**。

    合并键 = 消息内位置。`telegram_file_id` 是消息级定位符
    (`tl:{chat_id}:{msg_id}`,同一条消息内所有 media 共享,见
    telethon_messages.py),无法区分条目,位置索引是本代码库既有的
    per-media 稳定语义(dto.py:thumbnails 的 media_idx 即按索引关联)。

    规则:
    - 位置匹配上的 media:下载侧字段(`object_key` / `object_backend` /
      `download_status` / `download_error`)保留旧值;远端侧字段(type /
      mime_type / file_name / 尺寸 / duration / file_id / thumb id /
      emoji)以新 DTO 为准;`file_size` 新值非 None 用新值,否则保留旧值
      (编辑重映射可能拿不到 size,不能倒退抹掉)。
    - 超出旧列表的新 media:按未下载新条目插入(交由下载队列处理)。
    - 旧列表多出的 media(编辑后消失):保守保留、追加尾部。删除会让
      已下载对象立即变孤儿被 reconcile 回收;保留的代价是这些条目的
      thumbnail `media_idx` 可能与 thumbnails 表错位(缩略图可按需重下,
      可接受)。TG 编辑实际不会移除媒体,此分支纯防御。
    """
    merged: list[MediaDTO] = []
    for idx, new in enumerate(new_media):
        if idx < len(old_media):
            old = old_media[idx]
            merged.append(
                dataclasses.replace(
                    new,
                    file_size=new.file_size if new.file_size is not None else old.file_size,
                    object_key=old.object_key,
                    object_backend=old.object_backend,
                    download_status=old.download_status,
                    download_error=old.download_error,
                )
            )
        else:
            merged.append(new)
    merged.extend(old_media[len(new_media) :])
    return merged


class MonitorService:
    """实时监听服务:订阅 TelegramClient.update 流 → 落库 + 发 MessageReceived。

    除了实时流,还会以 `_BACKFILL_INTERVAL` 周期对白名单频道做一轮补拉
    (见 `_backfill`):Telegram 实时推送只覆盖连接期间的新消息,
    断线 / 重启期间的消息不会重放,补拉负责兜底,避免监测不全 / 长空窗。

    显式组合 5 个组件(WhitelistManager / Orchestrator / BackfillRunner /
    EventHandlers / DeleteManager),状态单一所有权在本类;组件经 `svc`
    引用读写状态。公开 / 测试 seam 方法以 delegate 转发到组件,调用面不变。
    """

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
        # 共享 runtime:依赖 + 全部可变状态单一所有权在此(P2-M4)。
        self._rt = MonitorRuntime(
            bus=bus,
            client=client,
            storage=storage,
            objects=objects,
            settings=settings,
            downloader=downloader,
            heartbeat_interval=_DEFAULT_HEARTBEAT_INTERVAL,
            backfill_interval=_DEFAULT_BACKFILL_INTERVAL,
            backfill_limit=_DEFAULT_BACKFILL_LIMIT,
            backfill_max_page=_DEFAULT_BACKFILL_MAX_PAGE,
        )
        rt = self._rt

        # 组合组件 — 只持共享 runtime,不再反向持有宿主。
        self._whitelist_mgr = WhitelistManager(rt)
        self._orchestrator = Orchestrator(rt)
        self._backfill_runner = BackfillRunner(rt)
        self._events = EventHandlers(rt)
        self._delete_mgr = DeleteManager(rt)

        # 跨组件晚绑定回调 — lambda 解析本宿主**当前**属性,使测试在
        # monitor 实例上 patch `_backfill_all` / `_delete_with_orphan_check`
        # 等方法后,组件调用仍命中 patch(保持 seam 语义不变)。
        rt.handle_message = lambda msg: self._handle(msg)
        rt.handle_edited = lambda msg: self._handle_edited(msg)
        rt.backfill_all = lambda: self._backfill_all()
        rt.download_worker = lambda: self._download_worker()
        rt.delete_with_orphan_check = lambda cid, mid: self._delete_with_orphan_check(cid, mid)
        rt.handle_interactions_changed = lambda e: self._handle_interactions_changed(e)
        rt.handle_pin_changed = lambda e: self._handle_pin_changed(e)
        rt.handle_message_deleted = lambda e: self._handle_message_deleted(e)
        rt.handle_channel_metadata = lambda e: self._handle_channel_metadata(e)
        rt.handle_channel_title_changed = lambda e: self._handle_channel_title_changed(e)
        rt.handle_channel_photo_changed = lambda e: self._handle_channel_photo_changed(e)
        rt.handle_connection_state = lambda e: self._handle_connection_state(e)
        rt.backfill_loop = lambda: self._backfill_loop()

    # ---------- 共享状态 property(P2-M4:转发 runtime,seam 位置不变) ----------

    @property
    def bus(self) -> EventBus:
        return self._rt.bus

    @property
    def client(self) -> TelegramClient:
        return self._rt.client

    @property
    def storage(self) -> StorageRepository:
        return self._rt.storage

    @storage.setter
    def storage(self, value: StorageRepository) -> None:
        self._rt.storage = value

    @property
    def objects(self) -> ObjectStore:
        return self._rt.objects

    @objects.setter
    def objects(self, value: ObjectStore) -> None:
        self._rt.objects = value

    @property
    def settings(self) -> Settings:
        return self._rt.settings

    @settings.setter
    def settings(self, value: Settings) -> None:
        self._rt.settings = value

    @property
    def downloader(self) -> MediaDownloader | None:
        return self._rt.downloader

    @downloader.setter
    def downloader(self, value: MediaDownloader | None) -> None:
        self._rt.downloader = value

    @property
    def _whitelist(self) -> set[int]:
        return self._rt.whitelist

    @property
    def _seen_ids(self) -> collections.OrderedDict[tuple[int, int], None]:
        return self._rt.seen_ids

    @property
    def _download_queue(self) -> asyncio.Queue[tuple[MessageDTO, int]] | None:
        return self._rt.download_queue

    @_download_queue.setter
    def _download_queue(self, value: asyncio.Queue[tuple[MessageDTO, int]] | None) -> None:
        self._rt.download_queue = value

    @property
    def _handled(self) -> int:
        return self._rt.handled

    @_handled.setter
    def _handled(self, value: int) -> None:
        self._rt.handled = value

    @property
    def _skip_next_tick(self) -> bool:
        return self._rt.skip_next_tick

    @_skip_next_tick.setter
    def _skip_next_tick(self, value: bool) -> None:
        self._rt.skip_next_tick = value

    @property
    def _unavailable_channels(self) -> set[int]:
        return self._rt.unavailable_channels

    @property
    def _last_heartbeat(self) -> float:
        return self._rt.last_heartbeat

    @_last_heartbeat.setter
    def _last_heartbeat(self, value: float) -> None:
        self._rt.last_heartbeat = value

    @property
    def _stop(self) -> asyncio.Event:
        return self._rt.stop

    @property
    def _stream(self) -> UpdateStream | None:
        return self._rt.stream

    @_stream.setter
    def _stream(self, value: UpdateStream | None) -> None:
        self._rt.stream = value

    @property
    def _task(self) -> asyncio.Task | None:
        return self._rt.task

    @_task.setter
    def _task(self, value: asyncio.Task | None) -> None:
        self._rt.task = value

    @property
    def _backfill_task(self) -> asyncio.Task | None:
        return self._rt.backfill_task

    @_backfill_task.setter
    def _backfill_task(self, value: asyncio.Task | None) -> None:
        self._rt.backfill_task = value

    @property
    def _download_task(self) -> asyncio.Task | None:
        return self._rt.download_task

    @_download_task.setter
    def _download_task(self, value: asyncio.Task | None) -> None:
        self._rt.download_task = value

    @property
    def _BACKFILL_INTERVAL(self) -> float:  # noqa: N802 — 测试 seam 沿用原名
        return self._rt.backfill_interval

    @_BACKFILL_INTERVAL.setter
    def _BACKFILL_INTERVAL(self, value: float) -> None:  # noqa: N802 — 测试 seam setter
        self._rt.backfill_interval = value

    @property
    def _BACKFILL_LIMIT(self) -> int:  # noqa: N802 — 测试 seam 沿用原名
        return self._rt.backfill_limit

    @_BACKFILL_LIMIT.setter
    def _BACKFILL_LIMIT(self, value: int) -> None:  # noqa: N802 — 测试 seam setter
        self._rt.backfill_limit = value

    @property
    def _BACKFILL_MAX_PAGE(self) -> int:  # noqa: N802 — 测试 seam 沿用原名
        return self._rt.backfill_max_page

    @_BACKFILL_MAX_PAGE.setter
    def _BACKFILL_MAX_PAGE(self, value: int) -> None:  # noqa: N802 — 测试 seam setter
        self._rt.backfill_max_page = value

    @property
    def _HEARTBEAT_INTERVAL(self) -> float:  # noqa: N802 — 测试 seam 沿用原名
        return self._rt.heartbeat_interval

    @_HEARTBEAT_INTERVAL.setter
    def _HEARTBEAT_INTERVAL(self, value: float) -> None:  # noqa: N802 — 测试 seam setter
        self._rt.heartbeat_interval = value

    # ---------- 白名单 delegate(WhitelistManager)----------

    def set_whitelist(self, channel_ids: Iterable[int]) -> None:
        """替换白名单 — 转发 WhitelistManager。"""
        self._whitelist_mgr.set_whitelist(channel_ids)

    def add_to_whitelist(self, channel_id: int) -> None:
        """增量加频道到白名单 — 转发 WhitelistManager。"""
        self._whitelist_mgr.add_to_whitelist(channel_id)

    def remove_from_whitelist(self, channel_id: int) -> None:
        """从白名单摘频道 — 转发 WhitelistManager。"""
        self._whitelist_mgr.remove_from_whitelist(channel_id)

    @property
    def monitored_ids(self) -> frozenset[int]:
        """订阅频道 id 快照 — 转发 WhitelistManager。"""
        return self._whitelist_mgr.monitored_ids

    async def update_backends(
        self,
        storage: StorageRepository,
        objects: ObjectStore,
        settings: Settings,
    ) -> None:
        """热重载同步后端 — 转发 WhitelistManager。"""
        await self._whitelist_mgr.update_backends(storage, objects, settings)

    # ---------- 生命周期 delegate(Orchestrator)----------

    async def start(self) -> None:
        """启动主循环 — 转发 Orchestrator。"""
        await self._orchestrator.start()

    async def stop(self) -> None:
        """停 monitor — 转发 Orchestrator。"""
        await self._orchestrator.stop()

    # ---------- 补拉 delegate(BackfillRunner)----------

    async def _backfill(self, channel_id: int) -> None:
        """补拉单频道 — 转发 BackfillRunner。"""
        await self._backfill_runner._backfill(channel_id)

    async def _backfill_all(self) -> None:
        """扫白名单补拉一轮 — 转发 BackfillRunner(测试可替换 seam)。"""
        await self._backfill_runner._backfill_all()

    async def _backfill_loop(self) -> None:
        """周期补拉主循环 — 转发 BackfillRunner。"""
        await self._backfill_runner._backfill_loop()

    async def _handle_connection_state(self, event: ConnectionStateChanged) -> None:
        """reconnect kick 补拉 — 转发 BackfillRunner。"""
        await self._backfill_runner._handle_connection_state(event)

    # ---------- bus 事件 handler delegate(EventHandlers / DeleteManager)----------

    async def _handle_interactions_changed(self, event: MessageInteractionsChanged) -> None:
        """reactions / views 增量 — 转发 EventHandlers。"""
        await self._events._handle_interactions_changed(event)

    async def _handle_pin_changed(self, event: MessagePinChanged) -> None:
        """server-side pin — 转发 EventHandlers。"""
        await self._events._handle_pin_changed(event)

    async def _handle_channel_metadata(self, event: ChannelMetadataChanged) -> None:
        """频道元数据增量 — 转发 EventHandlers。"""
        await self._events._handle_channel_metadata(event)

    async def _handle_channel_title_changed(self, event: ChannelTitleChanged) -> None:
        """chat 改名 — 转发 EventHandlers。"""
        await self._events._handle_channel_title_changed(event)

    async def _handle_channel_photo_changed(self, event: ChannelPhotoChanged) -> None:
        """chat 改头像 — 转发 EventHandlers。"""
        await self._events._handle_channel_photo_changed(event)

    async def _handle_message_deleted(self, event: MessageDeleted) -> None:
        """TG 端删消息 — 转发 DeleteManager。"""
        await self._delete_mgr._handle_message_deleted(event)

    # ---------- 删除 delegate(DeleteManager)----------

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """删单条 + 清孤儿 + 发 MessageDeleted — 转发 DeleteManager。"""
        await self._delete_mgr.delete_message(channel_id, telegram_msg_id)

    async def delete_messages(
        self,
        items: list[tuple[int, int]],
        cancel_event: asyncio.Event | None = None,
    ) -> tuple[int, list[tuple[int, int, str]]]:
        """批量删(支持 cancel_event)— 转发 DeleteManager。

        2026-11-25 P2 #32:返回 `(succeeded, failures)`,failures 供
        BatchService 组装 BatchDone.failures。
        """
        return await self._delete_mgr.delete_messages(items, cancel_event=cancel_event)

    async def _delete_with_orphan_check(self, channel_id: int, telegram_msg_id: int) -> None:
        """删 row + refcount + 孤儿 bytes — 转发 DeleteManager(测试可替换 seam)。"""
        await self._delete_mgr._delete_with_orphan_check(channel_id, telegram_msg_id)

    # ---- 核心消息流(_handle / _handle_edited / _download_worker) ----
    # 注:3 个 method 互相依赖(共享 _seen_ids / 共享下载队列 / 共享 _handled),
    # 故意不拆到组件 —— 拆开会让 delegate 转发 3 层,反而增加阅读跳数。

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
        # 拷 object_key / object_backend / file_size,后续未下载/DOWNLOADING +
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

        # 媒体下载策略(2026-11-22 v2.0.9 FR-09):
        #   METADATA  → 跳过 thumb / full(CLI 兼容的最小元数据档)
        #   THUMBNAIL → 消息落库后同步下缩略图(见 save_message 之后)
        #   FULL      → 缩略图 + 原文件由 _download_worker 下(见下)
        # 缩略图按用户语义裁决属「元数据侧」,默认档(THUMBNAIL)也要拿到。
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
                # None=新消息(未下载);DOWNLOADING=上次运行中断遗留 → 重启后重新下载;
                # DONE / FAILED 不再重下(FAILED 让用户看原因,不无限重试)。
                # 注:跨消息去重(D8 上面那段)已把已下载的 media 标 DONE +
                # object_key 已填,这里 `not object_key` 自然跳过。
                if (
                    med.download_status in (None, MediaDownloadStatus.DOWNLOADING)
                    and not med.object_key
                ):
                    med.download_status = MediaDownloadStatus.DOWNLOADING
                    med.download_error = None
                    queued.append(idx)

        # 幂等落库(FULL 模式下 media 状态由异步下载队列写回,`save_message`
        # 是 upsert:InMemoryRepository 是 dict 覆写,sqlite / postgres
        # 各仓也按 (channel_id, telegram_msg_id) 更新)。
        await self.storage.save_message(msg)

        # 2026-11-22 v2.0.9 FR-09(D8=β):默认档(THUMBNAIL,元数据侧)缩略图补齐。
        # 两个必须遵守的约束:
        # 1) 外键时序 —— thumbnails 表 REFERENCES messages,必须 save_message
        #    之后才能 save_thumbnail,否则外键约束失败(PG / SQLite 均如此);
        # 2) FULL 去重 —— 仅 THUMBNAIL 档走这条同步路径;FULL 档缩略图只由
        #    _download_worker 下(下方 :466),避免同一媒体下两次。
        # 失败隔离沿用 _maybe_download_thumb 的 try/except(仅 log,不阻断主流程)。
        if msg.media and self.settings.media_policy == MediaPolicy.THUMBNAIL:
            for idx, med in enumerate(msg.media):
                await self._maybe_download_thumb(msg, idx, med)

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
        写回 storage(`update_message`)并发 `MessageEdited` 事件。media
        按 `_merge_media_on_edit` 合并(保留下载侧状态),其余字段覆盖。

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
        # media 走合并(P1-1):整列表替换会把重新映射 DTO 的
        # object_key/download_status=None 写库,抹掉已下载状态 → 孤儿回收
        # 真删文件。见 `_merge_media_on_edit` docstring。
        updated = dataclasses.replace(
            existed,
            text=msg.text,
            views=msg.views,
            forwards=msg.forwards,
            edited=msg.edited,
            media=_merge_media_on_edit(existed.media, msg.media),
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
                """Telegram 进度回调 → 发 MediaDownloadProgress 事件。

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
            except Exception as e:
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
                updated.download_status.value if updated.download_status else "未下载",
                updated.object_key or "-",
            )
            # P2 #36:定向回写 media 行 — 不再 save_message 整条覆盖。
            # 下载可能长达 30 分钟,期间并发编辑推送(updateMessageContent)
            # 已把新 text / views 落库;整条回写会用下载开始时的旧 DTO 把
            # 它们覆盖回去。改为:重读当前消息 → 只替换 media[idx] →
            # `update_message_media`(DELETE+重插 media 行,text 不动)。
            # 消息在下载期间被删(get_message 返 None)则跳过回写。
            try:
                current = await self.storage.get_message(msg.channel_id, msg.telegram_msg_id)
                if current is not None:
                    current_media = list(current.media)
                    if idx < len(current_media):
                        current_media[idx] = updated
                    else:
                        current_media.append(updated)
                    await self.storage.update_message_media(
                        msg.channel_id,
                        msg.telegram_msg_id,
                        current_media,
                    )
                else:
                    log.warning(
                        "skip media writeback: message gone: channel=%s msg_id=%s",
                        msg.channel_id,
                        msg.telegram_msg_id,
                    )
            except Exception as e:
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
        except Exception:
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
        except Exception:
            log.exception(
                "save_thumbnail failed: channel=%s msg_id=%s idx=%d",
                msg.channel_id,
                msg.telegram_msg_id,
                idx,
            )

    # ---- helpers ----

    # 2026-08-25 PR #3:删除 `_count_media_with_object_key` — 由
    # `storage.count_media_by_object_key` 替代,后端走 SQL 索引 O(1),
    # InMemory 走顺序扫(与旧实现等价)。
    # 2026-11-22 v2.0.9 FR-09:删除 `_maybe_store_thumb` 空 hook —— 默认档缩略图
    # 现走 `_maybe_download_thumb`(上方),FULL 档由 `_download_worker` 下。
