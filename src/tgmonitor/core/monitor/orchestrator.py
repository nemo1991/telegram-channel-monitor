"""主循环 + 心跳 mixin — `MonitorService` 的 `start` / `stop` / `_run` /
`_log_heartbeat`。

2026-10-10 v1.12.x plan-batch2 目标 2 从 `MonitorService` 抽出:
主循环是热路径核心,但实际方法体本身不长(stop ~30 LOC / start ~40 LOC /
_run ~70 LOC / _log_heartbeat ~17 LOC)。抽出动机不是 LOC,而是
**「消息处理 vs 编排」关注点分离**:
  - orchestrator.py:task 生命周期、backoff 退避、心跳、stream 订阅/重订
  - service.py:消息解析、跨消息去重、落库、下载入队

设计:同 `WhitelistMixin` — mixin 期望宿主类持有
`self.client / bus / storage / settings / downloader / _stream / _task
/ _backfill_task / _download_task / _download_queue / _stop / _seen_ids
/ _handled / _last_heartbeat`,以及 `self._handle / self._handle_edited`
两条核心消息流入口(`_run` 主循环按 `_seen_ids` 路由)。
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import TYPE_CHECKING

from tgmonitor.core.dto import MessageDTO
from tgmonitor.core.events import (
    ChannelMetadataChanged,
    ChannelPhotoChanged,
    ChannelTitleChanged,
    ConnectionStateChanged,
    ErrorOccurred,
    MessageDeleted,
    MessageInteractionsChanged,
    MessagePinChanged,
)
from tgmonitor.core.telegram.client import UpdateStream

if TYPE_CHECKING:
    from tgmonitor.core.config import Settings
    from tgmonitor.core.events import EventBus
    from tgmonitor.core.monitor.downloader import MediaDownloader
    from tgmonitor.core.objectstore.base import ObjectStore
    from tgmonitor.core.storage.repository import StorageRepository
    from tgmonitor.core.telegram.client import TelegramClient

log = logging.getLogger("tgmonitor.core.monitor.orchestrator")


class OrchestratorMixin:
    """主循环 + 生命周期 mixin。集中 4 个 method:

    - `start` — 启动主循环 task + 补拉 task + 异步下载 task + bus 订阅
    - `stop` — 逆序:停下载 → 停补拉 → 关流 → 停主
    - `_run` — 消息主循环(heartbeat + 退避重连)
    - `_log_heartbeat` — 周期 INFO 心跳

    依赖宿主属性:见 module docstring。
    """

    bus: EventBus
    client: TelegramClient
    storage: StorageRepository
    objects: ObjectStore
    downloader: MediaDownloader | None
    settings: Settings

    # 周期补拉参数(测试可 monkeypatch 调小)
    _BACKFILL_INTERVAL: float
    _BACKFILL_LIMIT: int
    _BACKFILL_MAX_PAGE: int
    _HEARTBEAT_INTERVAL: float

    # 状态字段(在 MonitorService.__init__ 初始化,mixin 只用不声明类型,
    # 这里补声明让 mypy 别把 create_task(...).asyncio 返回类型 Task[None]
    # 当成 mixin 属性类型)。
    _task: asyncio.Task[None] | None
    _backfill_task: asyncio.Task[None] | None
    _download_task: asyncio.Task[None] | None
    _stream: UpdateStream | None
    _download_queue: asyncio.Queue[tuple[MessageDTO, int]] | None
    _whitelist: set[int]
    _stop: asyncio.Event
    _handled: int
    _last_heartbeat: float
    _skip_next_tick: bool
    _unavailable_channels: set[int]
    _seen_ids: collections.OrderedDict[tuple[int, int], None]

    # 调用宿主方法(在 MonitorService 里实现,不在 mixin 拆提 — 3 个
    # 互相依赖的热路径 method 故意放一起)。声明让 mypy 知道契约存在。
    # 注:不在这里写 `async def _handle ...: ...` —— 那会**重写** mixin
    # 提供的同名方法并被 Python 实际选中(导致 bus.subscribe 路径调到
    # 哑 stub)。改用 `if TYPE_CHECKING` 块的 `var: Callable` 仅作类型。
    # 实际:MonitorService 显式定义 3 个 method,其它 mixin 提供 handler,
    # MRO 自然按 mixin 声明顺序解析到正确实现。这里仅注释契约即可,
    # mypy 不会因为 mixin 缺声明而报错(因为这些 method 实际是 mixin
    # 自有,只要不被 stub 覆盖即可)。

    # 跨 mixin 周期任务:BackfillMixin 提供 `_backfill_loop`,orchestrator 启动它。
    # 同上:`async def ...: ...` 会覆盖 backfill 的真实现。mypy 解析时
    # 若需要该 method,在调用点已能通过 backfill 看到 — 不需要此处声明。

    async def start(self) -> None:
        """启动主循环;幂等(已启动时 no-op)。"""
        if self._task is not None:
            return
        self._stop.clear()
        self._seen_ids.clear()
        self._stream = self.client.subscribe_updates()
        self._task = asyncio.create_task(self._run(), name="MonitorService")
        # 跨 mixin 方法:`_backfill_loop` 在 BackfillMixin;mypy 看不到 —
        # 运行时 MRO 解析正确
        self._backfill_task = asyncio.create_task(
            self._backfill_loop(),  # type: ignore[attr-defined]
            name="MonitorService.backfill",
        )
        # 2026-08-27 v1.4.0 PR #10:订阅 reactions / views 增量更新事件。
        # TDLib 高频推 updateMessageInteractionInfo,落库走 bus → 单点 +
        # 订阅者异常被吞,比 stream 更安全。
        self.bus.subscribe(
            MessageInteractionsChanged,
            self._handle_interactions_changed,  # type: ignore[attr-defined]
        )
        # 2026-09-10 v1.7.3:TDLib `updateMessageIsPinned` 推送 → 落库 + republish
        # MessageEdited 触发 UI 行 📌 刷新(只有 republish 才能让 UI 实时感知
        # 外部 pin,而不依赖下次 reload)。
        self.bus.subscribe(
            MessagePinChanged,
            self._handle_pin_changed,  # type: ignore[attr-defined]
        )
        # 2026-08-27 v1.4.0 PR #11:订阅 TG 端消息删除事件。落库删 row +
        # 减 object_key refcount,与 `delete_message` 路径同语义。
        self.bus.subscribe(
            MessageDeleted,
            self._handle_message_deleted,  # type: ignore[attr-defined]
        )
        # 2026-08-27 v1.4.0 PR #14:订阅频道元数据变更事件。`updateChannel`
        # 直接落库;`updateSupergroup` 需要先查 channel_id(用户名匹配)。
        self.bus.subscribe(
            ChannelMetadataChanged,
            self._handle_channel_metadata,  # type: ignore[attr-defined]
        )
        # 2026-09-03 v1.6.0 PR #Q2:订阅普通 chat 改名 / 改头像事件。直接落库
        # (channel_id 即 chat_id,无需反查);UI 端 `ChannelListCard` 也会
        # 订阅同一事件实时刷新卡片。
        self.bus.subscribe(
            ChannelTitleChanged,
            self._handle_channel_title_changed,  # type: ignore[attr-defined]
        )
        self.bus.subscribe(
            ChannelPhotoChanged,
            self._handle_channel_photo_changed,  # type: ignore[attr-defined]
        )
        self.bus.subscribe(
            ConnectionStateChanged,
            self._handle_connection_state,  # type: ignore[attr-defined]
        )
        # 异步下载 worker 仅在接了 MediaDownloader 时启动(FULL 策略)。
        if self.downloader is not None:
            self._download_queue = asyncio.Queue()
            # 跨 mixin 方法:`_download_worker` 在 MonitorService 主类
            # (3 个互相依赖的热路径 method 故意放一起,不抽 mixin)
            self._download_task = asyncio.create_task(
                self._download_worker(),  # type: ignore[attr-defined]
                name="MonitorService.download",
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
                            await self._handle_edited(msg)  # type: ignore[attr-defined]
                        else:
                            await self._handle(msg)  # type: ignore[attr-defined]
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
