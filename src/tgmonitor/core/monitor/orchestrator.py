"""主循环 + 心跳组件 — `MonitorService` 的 `start` / `stop` / `_run` /
`_log_heartbeat`。

2026-11-11 v2.0.0 Phase 3-C3:从 `OrchestratorMixin` 改为显式组合组件。
组件持 `svc: MonitorService` 引用,状态单一所有权仍在 MonitorService,
组件经 `self._svc.*` 读写 — 不再有 mixin MRO 魔法 / `type: ignore[attr-defined]`。

关注点分离:
  - orchestrator.py:task 生命周期、backoff 退避、心跳、stream 订阅/重订
  - service.py:消息解析、跨消息去重、落库、下载入队

跨组件接线一律经宿主 delegate:`start` 启动 `self._svc._backfill_loop()` /
`self._svc._download_worker()`,bus 订阅 `self._svc._handle_*`;`_run` 按
`_seen_ids` 路由到 `self._svc._handle` / `self._svc._handle_edited`。
本组件自有方法(`_run` / `_log_heartbeat`)仍走 `self.` 直调。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger("tgmonitor.core.monitor.orchestrator")


class Orchestrator:
    """主循环 + 生命周期组件。集中 4 个 method:

    - `start` — 启动主循环 task + 补拉 task + 异步下载 task + bus 订阅
    - `stop` — 逆序:停下载 → 停补拉 → 关流 → 停主
    - `_run` — 消息主循环(heartbeat + 退避重连)
    - `_log_heartbeat` — 周期 INFO 心跳

    经 `self._svc` 访问宿主状态:client / bus / storage / settings /
    downloader / _stream / _task / _backfill_task / _download_task /
    _download_queue / _stop / _seen_ids / _handled / _last_heartbeat /
    _whitelist 及类常量 _HEARTBEAT_INTERVAL。
    """

    def __init__(self, svc: MonitorService) -> None:
        self._svc = svc

    async def start(self) -> None:
        """启动主循环;幂等(已启动时 no-op)。"""
        svc = self._svc
        if svc._task is not None:
            return
        svc._stop.clear()
        svc._seen_ids.clear()
        svc._stream = svc.client.subscribe_updates()
        svc._task = asyncio.create_task(self._run(), name="MonitorService")
        # 跨组件:backfill loop 经宿主 delegate 启动(BackfillRunner 实现)
        svc._backfill_task = asyncio.create_task(
            svc._backfill_loop(),
            name="MonitorService.backfill",
        )
        # 2026-08-27 v1.4.0 PR #10:订阅 reactions / views 增量更新事件。
        # TDLib 高频推 updateMessageInteractionInfo,落库走 bus → 单点 +
        # 订阅者异常被吞,比 stream 更安全。
        svc.bus.subscribe(
            MessageInteractionsChanged,
            svc._handle_interactions_changed,
        )
        # 2026-09-10 v1.7.3:TDLib `updateMessageIsPinned` 推送 → 落库 + republish
        # MessageEdited 触发 UI 行 📌 刷新(只有 republish 才能让 UI 实时感知
        # 外部 pin,而不依赖下次 reload)。
        svc.bus.subscribe(
            MessagePinChanged,
            svc._handle_pin_changed,
        )
        # 2026-08-27 v1.4.0 PR #11:订阅 TG 端消息删除事件。落库删 row +
        # 减 object_key refcount,与 `delete_message` 路径同语义。
        svc.bus.subscribe(
            MessageDeleted,
            svc._handle_message_deleted,
        )
        # 2026-08-27 v1.4.0 PR #14:订阅频道元数据变更事件。`updateChannel`
        # 直接落库;`updateSupergroup` 需要先查 channel_id(用户名匹配)。
        svc.bus.subscribe(
            ChannelMetadataChanged,
            svc._handle_channel_metadata,
        )
        # 2026-09-03 v1.6.0 PR #Q2:订阅普通 chat 改名 / 改头像事件。直接落库
        # (channel_id 即 chat_id,无需反查);UI 端 `ChannelListCard` 也会
        # 订阅同一事件实时刷新卡片。
        svc.bus.subscribe(
            ChannelTitleChanged,
            svc._handle_channel_title_changed,
        )
        svc.bus.subscribe(
            ChannelPhotoChanged,
            svc._handle_channel_photo_changed,
        )
        svc.bus.subscribe(
            ConnectionStateChanged,
            svc._handle_connection_state,
        )
        # 异步下载 worker 仅在接了 MediaDownloader 时启动(FULL 策略)。
        if svc.downloader is not None:
            svc._download_queue = asyncio.Queue()
            # 跨组件:download worker 是 MonitorService 核心热路径方法
            svc._download_task = asyncio.create_task(
                svc._download_worker(),
                name="MonitorService.download",
            )
        log.info(
            "MonitorService started; whitelist size=%d",
            len(svc._whitelist),
        )

    async def stop(self) -> None:
        """停 monitor + 关流 + 等 task 退出(2s 超时硬 cancel)。"""
        svc = self._svc
        svc._stop.set()
        if svc._download_task is not None:
            svc._download_task.cancel()
            try:
                await svc._download_task
            except asyncio.CancelledError:
                pass
            svc._download_task = None
        svc._download_queue = None
        if svc._backfill_task is not None:
            svc._backfill_task.cancel()
            try:
                await svc._backfill_task
            except asyncio.CancelledError:
                pass
            svc._backfill_task = None
        if svc._stream is not None:
            try:
                await svc._stream.aclose()
            except Exception:  # noqa: BLE001
                pass
        if svc._task is not None:
            try:
                await asyncio.wait_for(svc._task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                svc._task.cancel()
            svc._task = None

    async def _run(self) -> None:
        svc = self._svc
        assert svc._stream is not None
        backoff = 1.0
        while not svc._stop.is_set():
            try:
                while not svc._stop.is_set():
                    try:
                        # 用 timeout 包住 anext:流静默超过 _HEARTBEAT_INTERVAL
                        # 秒时抛 TimeoutError → 打心跳后继续等。
                        async with asyncio.timeout(svc._HEARTBEAT_INTERVAL):
                            msg = await anext(svc._stream)
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
                        if key in svc._seen_ids:
                            await svc._handle_edited(msg)
                        else:
                            await svc._handle(msg)
                    except Exception as e:  # noqa: BLE001
                        log.exception("handle message failed: %s", e)
                        await svc.bus.publish(
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
                await svc.bus.publish(
                    ErrorOccurred(source="monitor.loop", message=str(e), exception=e)
                )
                # 退避后重新订阅
                try:
                    await asyncio.wait_for(svc._stop.wait(), timeout=backoff)
                    break  # 停止事件触发
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)
                try:
                    if svc._stream is not None:
                        await svc._stream.aclose()
                except Exception:  # noqa: BLE001
                    pass
                svc._stream = svc.client.subscribe_updates()

    def _log_heartbeat(self, *, no_updates: bool) -> None:
        """周期心跳 INFO 日志:距上次心跳 ≥ `_HEARTBEAT_INTERVAL` 才打。

        `no_updates=True` 表示该次由流静默超时触发(距上次消息已超过一个周期);
        `False` 表示流有消息、只是到点顺带汇报一次。两者都证明实时通道活着。
        INFO 级别是为了默认可见 —— 用户不需要设 `TG_LOG_LEVEL=DEBUG` 就能
        确认 monitor 是否在心跳(30s 一条不算日志噪音)。
        """
        svc = self._svc
        now = time.monotonic()
        if now - svc._last_heartbeat < svc._HEARTBEAT_INTERVAL:
            return
        svc._last_heartbeat = now
        log.info(
            "monitor heartbeat: stream alive%s (whitelist=%d, handled=%d)",
            ", no updates" if no_updates else "",
            len(svc._whitelist),
            svc._handled,
        )
