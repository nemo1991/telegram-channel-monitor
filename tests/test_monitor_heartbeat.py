"""2026-09-17 PR 4 mega-split:MonitorService heartbeat + interactions 路由测试。

5 tests:
- 实时流 idle 日志(1):N 秒无 update → INFO「stream idle」
- update 收到即打 INFO + 落库(1):update_received → storage 已存 + 日志有「received」
- 实时流活跃(1):5s 内有 update → 不打 idle 日志
- interactions_changed 路由(1):→ storage.update_interactions + publish MessageEdited
- interactions handler 异常吞(1):storage 抛 → 不冒泡
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from tests.conftest import make_message
from tgmonitor.core.dto import MessageDTO
from tgmonitor.core.events import MessageInteractionsChanged


async def test_monitor_heartbeat_logs_when_stream_idle(monitor, client, caplog) -> None:
    """实时流 idle(>HEARTBEAT_INTERVAL 无 update)→ 监控 logger 打 INFO
    「heartbeat: stream alive, no updates」— 用户据此确认 monitor 在跑。

    让 monitor 启动后不发任何 update 等过 1 个 heartbeat 周期 → 抓日志断言。
    """

    monitor._HEARTBEAT_INTERVAL = 0.1  # type: ignore[assignment]
    monitor.set_whitelist([100])
    with caplog.at_level(logging.INFO, logger="tgmonitor.core.monitor.service"):
        await monitor.start()
        try:
            # 等 2 个周期确保 heartbeat 被记到
            await asyncio.sleep(0.25)
        finally:
            await monitor.stop()
    # 日志里至少 1 次 "heartbeat" 且含 "no updates"
    assert "heartbeat" in caplog.text.lower()
    assert "no updates" in caplog.text.lower()


async def test_monitor_logs_update_received_and_stored(
    monitor, client, bus, caplog, storage
) -> None:
    """实时收到 update → DEBUG 日志「update received」+ storage 落库。

    实际实现里 update received 是 DEBUG 级别(避免 1 msg/sec 频道日志洪水),
    所以测试在 DEBUG 级别抓。业务断言走 storage.get_message — 落库即收据。
    """

    monitor.set_whitelist([100])
    with caplog.at_level(logging.DEBUG, logger="tgmonitor.core.monitor.service"):
        await monitor.start()
        try:
            await client.simulate_incoming(make_message(channel_id=100, msg_id=1, text="hi"))
            # 等消息落库(走 monitor _handle path)
            from tests.fixtures._async import wait_for

            assert await wait_for(lambda: storage.get_message(100, 1), timeout=2.0), (
                "message 没在 2s 内落库"
            )
        finally:
            await monitor.stop()
    # DEBUG 日志有 "update received"
    assert "update received" in caplog.text.lower()
    # 业务断言:消息已落库
    stored = await storage.get_message(100, 1)
    assert stored is not None
    assert stored.text == "hi"


async def test_monitor_heartbeat_logs_when_stream_active(monitor, client, bus) -> None:
    """5s 内有 update → heartbeat 仍打「stream alive」(无 no updates 后缀)。

    与 test_monitor_heartbeat_logs_when_stream_idle 互为反例 — 验证 heartbeat
    区分 idle vs active,UI 借此判断流是否健康。
    """

    monitor._HEARTBEAT_INTERVAL = 0.1  # type: ignore[assignment]
    monitor.set_whitelist([100])
    caplog_records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda r: caplog_records.append(r.getMessage())
    logger = logging.getLogger("tgmonitor.core.monitor.service")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        await monitor.start()
        try:
            for i in range(5):
                await client.simulate_incoming(
                    make_message(channel_id=100, msg_id=i + 1, text=f"m{i}")
                )
                await asyncio.sleep(0)  # 让 monitor loop tick 处理
        finally:
            await monitor.stop()
    finally:
        logger.removeHandler(handler)
    # 活跃期间:有心跳日志,但每条都不含 "no updates"(有 update 时 no_updates=False)
    heartbeats = [m for m in caplog_records if "heartbeat" in m.lower()]
    assert heartbeats, "活跃期间也应有 heartbeat 日志(stream alive)"
    assert all("no updates" not in m.lower() for m in heartbeats), (
        "活跃期 heartbeat 不应带 'no updates' 后缀"
    )


async def test_monitor_routes_interactions_changed_to_storage(
    monitor, storage, client, bus
) -> None:
    """publish MessageInteractionsChanged → storage.update_message_interactions 被调
    (views=99 透传;handler **不** republish MessageEdited — 详情面板订阅
    原事件即可)。"""
    monitor.set_whitelist([100])
    # 预存一条消息
    await storage.save_message(
        MessageDTO(
            id=0,
            channel_id=100,
            telegram_msg_id=10,
            date=datetime(2026, 1, 1, tzinfo=UTC),
            text="x",
            author="alice",
            media=[],
        )
    )
    # 计数 storage.update_message_interactions 调用
    update_calls: list = []

    async def _spy(*args, **kwargs):
        update_calls.append((args, kwargs))

    monitor.storage.update_message_interactions = _spy  # type: ignore[attr-defined]

    await monitor.start()
    try:
        await bus.publish(
            MessageInteractionsChanged(
                channel_id=100,
                telegram_msg_id=10,
                views=99,
            )
        )
        # 等 handler 调 _handle_interactions_changed → storage.update_message_interactions
        from tests.fixtures._async import wait_for

        assert await wait_for(lambda: len(update_calls) >= 1, timeout=2.0), (
            "interactions handler 没在 2s 内调 storage"
        )
        # storage 被调 1 次,带正确参数(views=99,reactions=None)
        assert len(update_calls) == 1
        assert update_calls[0][0] == (100, 10)
        assert update_calls[0][1].get("views") == 99
    finally:
        await monitor.stop()


async def test_monitor_interactions_handler_swallows_errors(monitor, storage, bus) -> None:
    """MessageInteractionsChanged 触发时 storage 抛异常 → handler 不冒泡 + 发
    ErrorOccurred 事件(让 UI 弹错对话框)。"""
    await monitor.start()

    class BoomStorage:
        async def update_message_interactions(self, *a, **kw):
            raise RuntimeError("simulated")

    original = monitor.storage
    monitor.storage = BoomStorage()
    seen: list = []

    async def _on_err(e):
        seen.append(e)

    bus.subscribe(
        __import__("tgmonitor.core.events", fromlist=["ErrorOccurred"]).ErrorOccurred, _on_err
    )
    try:
        await bus.publish(
            MessageInteractionsChanged(
                channel_id=100,
                telegram_msg_id=1,
                views=1,
            )
        )
        # 等 handler 处理完(抛异常 → 发 ErrorOccurred 事件)
        from tests.fixtures._async import wait_for

        from tgmonitor.core.events import ErrorOccurred

        assert await wait_for(
            lambda: any(isinstance(e, ErrorOccurred) for e in seen), timeout=2.0
        ), "ErrorOccurred 没在 2s 内发出"
        # 异常被吞,ErrorOccurred 事件发 1 次
        from tgmonitor.core.events import ErrorOccurred

        assert any(isinstance(e, ErrorOccurred) and "simulated" in e.message for e in seen)
    finally:
        bus.unsubscribe(
            __import__("tgmonitor.core.events", fromlist=["ErrorOccurred"]).ErrorOccurred, _on_err
        )
        monitor.storage = original
        await monitor.stop()
