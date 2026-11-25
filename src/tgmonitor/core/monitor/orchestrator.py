"""主循环 + 心跳组件 — `MonitorService` 的 `start` / `stop` / `_run` /
`_log_heartbeat`。

2026-11-11 v2.0.0 Phase 3-C3:从 `OrchestratorMixin` 改为显式组合组件。
2026-11-25 P2-M4:组件改为持有共享 `MonitorRuntime`,不再反向持有
`MonitorService`;状态经 `self._rt.*` 读写,跨组件接线经晚绑定回调。

关注点分离:
  - orchestrator.py:task 生命周期、backoff 退避、心跳、stream 订阅/重订
  - service.py:消息解析、跨消息去重、落库、下载入队

跨组件接线经 rt 晚绑定回调:`start` 启动 `rt.backfill_loop()` /
`rt.download_worker()`,bus 订阅 `rt.handle_*`;`_run` 按
`seen_ids` 路由到 `rt.handle_message` / `rt.handle_edited`。
本组件自有方法(`_run` / `_log_heartbeat`)仍走 `self.` 直调。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

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
from tgmonitor.core.monitor._runtime import MonitorRuntime

log = logging.getLogger("tgmonitor.core.monitor.orchestrator")


def _require[C: Callable[..., Any]](cb: C | None) -> C:
    """取晚绑定回调 —— 由 MonitorService.__init__ 注入,start() 前必已就位。"""
    assert cb is not None
    return cb


class Orchestrator:
    """主循环 + 生命周期组件。集中 4 个 method:

    - `start` — 启动主循环 task + 补拉 task + 异步下载 task + bus 订阅
    - `stop` — 逆序:停下载 → 停补拉 → 关流 → 停主
    - `_run` — 消息主循环(heartbeat + 退避重连)
    - `_log_heartbeat` — 周期 INFO 心跳

    经 `self._rt` 访问共享状态:client / bus / downloader / stream /
    task / backfill_task / download_task / download_queue / stop /
    seen_ids / handled / last_heartbeat / whitelist 及 heartbeat_interval。
    """

    def __init__(self, rt: MonitorRuntime) -> None:
        self._rt = rt

    async def start(self) -> None:
        """启动主循环;幂等(已启动时 no-op)。"""
        rt = self._rt
        if rt.task is not None:
            return
        rt.stop.clear()
        rt.seen_ids.clear()
        rt.stream = rt.client.subscribe_updates()
        rt.task = asyncio.create_task(self._run(), name="MonitorService")
        # 跨组件:backfill loop 经宿主 delegate 启动(BackfillRunner 实现)
        rt.backfill_task = asyncio.create_task(
            _require(rt.backfill_loop)(),
            name="MonitorService.backfill",
        )
        # 2026-08-27 v1.4.0 PR #10:订阅 reactions / views 增量更新事件。
        # Telegram 高频推互动信息,落库走 bus → 单点 +
        # 订阅者异常被吞,比 stream 更安全。
        rt.bus.subscribe(
            MessageInteractionsChanged,
            _require(rt.handle_interactions_changed),
        )
        # 2026-09-10 v1.7.3:Telegram 推送消息 pin / unpin → 落库 + republish
        # MessageEdited 触发 UI 行 置顶 刷新(只有 republish 才能让 UI 实时感知
        # 外部 pin,而不依赖下次 reload)。
        rt.bus.subscribe(
            MessagePinChanged,
            _require(rt.handle_pin_changed),
        )
        # 2026-08-27 v1.4.0 PR #11:订阅 TG 端消息删除事件。落库删 row +
        # 减 object_key refcount,与 `delete_message` 路径同语义。
        rt.bus.subscribe(
            MessageDeleted,
            _require(rt.handle_message_deleted),
        )
        # 2026-08-27 v1.4.0 PR #14:订阅频道元数据变更事件。`updateChannel`
        # 直接落库;`updateSupergroup` 需要先查 channel_id(用户名匹配)。
        rt.bus.subscribe(
            ChannelMetadataChanged,
            _require(rt.handle_channel_metadata),
        )
        # 2026-09-03 v1.6.0 PR #Q2:订阅普通 chat 改名 / 改头像事件。直接落库
        # (channel_id 即 chat_id,无需反查);UI 端 `ChannelListCard` 也会
        # 订阅同一事件实时刷新卡片。
        rt.bus.subscribe(
            ChannelTitleChanged,
            _require(rt.handle_channel_title_changed),
        )
        rt.bus.subscribe(
            ChannelPhotoChanged,
            _require(rt.handle_channel_photo_changed),
        )
        rt.bus.subscribe(
            ConnectionStateChanged,
            _require(rt.handle_connection_state),
        )
        # 异步下载 worker 仅在接了 MediaDownloader 时启动(FULL 策略)。
        if rt.downloader is not None:
            rt.download_queue = asyncio.Queue()
            # 跨组件:download worker 是 MonitorService 核心热路径方法
            rt.download_task = asyncio.create_task(
                _require(rt.download_worker)(),
                name="MonitorService.download",
            )
        log.info(
            "MonitorService started; whitelist size=%d",
            len(rt.whitelist),
        )

    async def stop(self) -> None:
        """停 monitor + 关流 + 等 task 退出(2s 超时硬 cancel)。"""
        rt = self._rt
        rt.stop.set()
        if rt.download_task is not None:
            rt.download_task.cancel()
            try:
                await rt.download_task
            except asyncio.CancelledError:
                pass
            rt.download_task = None
        rt.download_queue = None
        if rt.backfill_task is not None:
            rt.backfill_task.cancel()
            try:
                await rt.backfill_task
            except asyncio.CancelledError:
                pass
            rt.backfill_task = None
        if rt.stream is not None:
            try:
                await rt.stream.aclose()
            except Exception:  # noqa: BLE001
                pass
        if rt.task is not None:
            try:
                await asyncio.wait_for(rt.task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                rt.task.cancel()
            rt.task = None

    async def _run(self) -> None:
        rt = self._rt
        assert rt.stream is not None
        backoff = 1.0
        while not rt.stop.is_set():
            try:
                while not rt.stop.is_set():
                    try:
                        # 用 timeout 包住 anext:流静默超过 _HEARTBEAT_INTERVAL
                        # 秒时抛 TimeoutError → 打心跳后继续等。
                        async with asyncio.timeout(rt.heartbeat_interval):
                            msg = await anext(rt.stream)
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
                        # Telegram 编辑推送走同一个 stream,
                        # 用 cache 命中标记。
                        key = (msg.channel_id, msg.telegram_msg_id)
                        if key in rt.seen_ids:
                            await _require(rt.handle_edited)(msg)
                        else:
                            await _require(rt.handle_message)(msg)
                    except Exception as e:  # noqa: BLE001
                        log.exception("handle message failed: %s", e)
                        await rt.bus.publish(
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
                await rt.bus.publish(
                    ErrorOccurred(source="monitor.loop", message=str(e), exception=e)
                )
                # 退避后重新订阅
                try:
                    await asyncio.wait_for(rt.stop.wait(), timeout=backoff)
                    break  # 停止事件触发
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)
                try:
                    if rt.stream is not None:
                        await rt.stream.aclose()
                except Exception:  # noqa: BLE001
                    pass
                rt.stream = rt.client.subscribe_updates()

    def _log_heartbeat(self, *, no_updates: bool) -> None:
        """周期心跳 INFO 日志:距上次心跳 ≥ `_HEARTBEAT_INTERVAL` 才打。

        `no_updates=True` 表示该次由流静默超时触发(距上次消息已超过一个周期);
        `False` 表示流有消息、只是到点顺带汇报一次。两者都证明实时通道活着。
        INFO 级别是为了默认可见 —— 用户不需要设 `TG_LOG_LEVEL=DEBUG` 就能
        确认 monitor 是否在心跳(30s 一条不算日志噪音)。
        """
        rt = self._rt
        now = time.monotonic()
        if now - rt.last_heartbeat < rt.heartbeat_interval:
            return
        rt.last_heartbeat = now
        log.info(
            "monitor heartbeat: stream alive%s (whitelist=%d, handled=%d)",
            ", no updates" if no_updates else "",
            len(rt.whitelist),
            rt.handled,
        )
