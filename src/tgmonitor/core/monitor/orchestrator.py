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
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from collections.abc import Awaitable

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


def _late_bound(
    getter: Callable[[], Callable[..., Awaitable[None]] | None],
) -> Callable[..., Awaitable[None]]:
    """把 bus 订阅包一层:调用时才解析回调槽。

    `rt.handle_*` 是晚绑定槽(测试会 `monitor._handle_pin = stub` 换实现),
    订阅只登记一次(见 `Orchestrator._subscribe_once`),所以不能在订阅时
    把回调对象取走 —— 每次事件到来再读槽,换实现后新实现立即生效。
    """

    async def _dispatch(event: Any) -> None:
        await _require(getter())(event)

    return _dispatch


class Orchestrator:
    """主循环 + 生命周期组件。集中 4 个 method:

    - `start` — 启动主循环 task + 补拉 task + 异步下载 task(幂等)
    - `stop` — 逆序:停下载 → 停补拉 → 关流 → 停主
    - `_run` — 消息主循环(heartbeat + 退避重连)
    - `_log_heartbeat` — 周期 INFO 心跳

    bus 订阅(`MessageInteractionsChanged` / `MessagePinChanged` /
    `MessageDeleted` / 频道元数据 / 连接状态)只在首次 `start()` 登记一次。

    经 `self._rt` 访问共享状态:client / bus / downloader / stream /
    task / backfill_task / download_task / download_queue / stop /
    seen_ids / handled / last_heartbeat / whitelist 及 heartbeat_interval。
    """

    def __init__(self, rt: MonitorRuntime) -> None:
        self._rt = rt
        # 2026-11-26 2.1.7 P1-2:bus 订阅只登记一次。旧实现每次 start() 都
        # 重新 subscribe、stop() 不退订 —— pause/resume 走 stop→start,
        # 每轮暂停/继续同一 handler 多一份,之后的消息被重复处理。
        self._subscribed = False

    def _subscribe_once(self) -> None:
        """登记 7 个 bus 订阅;重复调用 no-op(回调经 `_late_bound` 晚绑定)。"""
        if self._subscribed:
            return
        rt = self._rt
        rt.bus.subscribe(
            # Telegram 高频推互动信息,落库走 bus → 单点 + 订阅者异常被吞,
            # 比流内分发更安全。
            MessageInteractionsChanged,
            _late_bound(lambda: rt.handle_interactions_changed),
        )
        # Telegram 推送消息 pin / unpin → 落库 + republish MessageEdited
        # 触发 UI 行置顶刷新(只有 republish 才能让 UI 实时感知外部 pin)。
        rt.bus.subscribe(MessagePinChanged, _late_bound(lambda: rt.handle_pin_changed))
        # TG 端消息删除事件:落库删 row + 减 object_key refcount,
        # 与 `delete_message` 路径同语义。
        rt.bus.subscribe(
            MessageDeleted,
            _late_bound(lambda: rt.handle_message_deleted),
        )
        # 频道元数据变更:`updateChannel` 直接落库;`updateSupergroup` 需要
        # 先按用户名反查 channel_id。
        rt.bus.subscribe(
            ChannelMetadataChanged,
            _late_bound(lambda: rt.handle_channel_metadata),
        )
        # 普通 chat 改名 / 改头像事件:直接落库(channel_id 即 chat_id,
        # 无需反查);UI 端 `ChannelListCard` 也订阅同一事件实时刷新卡片。
        rt.bus.subscribe(
            ChannelTitleChanged,
            _late_bound(lambda: rt.handle_channel_title_changed),
        )
        rt.bus.subscribe(
            ChannelPhotoChanged,
            _late_bound(lambda: rt.handle_channel_photo_changed),
        )
        rt.bus.subscribe(
            ConnectionStateChanged,
            _late_bound(lambda: rt.handle_connection_state),
        )
        self._subscribed = True

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
        self._subscribe_once()
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
                # 2.1.7 P1-3:关流加超时 —— `_TelethonUpdateStream.close()`
                # 已改为满队列不阻塞,这里是给其它 UpdateStream 实现兜底,
                # 保证下方的"2s 硬 cancel"真的能到。
                await asyncio.wait_for(rt.stream.aclose(), timeout=2.0)
            except Exception as e:  # noqa: BLE001
                log.warning("monitor stream close failed or timed out: %s", e)
        if rt.task is not None:
            try:
                await asyncio.wait_for(rt.task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                # P2 #34:cancel 后必须 await gather — 旧实现只 cancel 不等,
                # task 的异常无人观察(asyncio "Task exception was never
                # retrieved"),且若 cancel 自身再被卡住,stop 直接跳走。
                # gather(return_exceptions=True) 收尾并吞掉一切结果。
                rt.task.cancel()
                try:
                    await asyncio.gather(rt.task, return_exceptions=True)
                except asyncio.CancelledError:
                    pass
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
                    except Exception as e:
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
            except Exception as e:
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
