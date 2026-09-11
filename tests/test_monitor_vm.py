"""2026-09-11 v1.7.4:`MonitorViewModel._on_message_interactions_changed` 单元测试。

覆盖:
- VM 在 `_wire_bus` 订阅了 `MessageInteractionsChanged`
- 收到事件后 emit `message_interactions_changed` signal(传给 MainWindow)
- reactions=None / 非 None 两种 payload 都透传(下游 `live_view.refresh_reactions` 自行处理 None)
"""

from __future__ import annotations

import asyncio

from tgmonitor.core.dto import ReactionDTO
from tgmonitor.core.events import EventBus, MessageInteractionsChanged
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel


class _FakeApp:
    """VM 只需要 `bus`;其它 AppService 接口 stub。"""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus


def _make_vm(bus: EventBus) -> MonitorViewModel:
    loop = asyncio.new_event_loop()
    return MonitorViewModel(_FakeApp(bus), monitor=None, loop=loop)  # type: ignore[arg-type]


async def test_vm_emits_signal_on_message_interactions_changed() -> None:
    """v1.7.4:VM 收到 `MessageInteractionsChanged` → emit `message_interactions_changed` signal。"""
    bus = EventBus()
    vm = _make_vm(bus)
    received: list[MessageInteractionsChanged] = []
    vm.message_interactions_changed.connect(lambda e: received.append(e))

    payload = MessageInteractionsChanged(
        channel_id=100,
        telegram_msg_id=42,
        reactions=[ReactionDTO(emoji="🔥", count=5)],
    )
    await bus.publish(payload)
    # 等 in-flight publish task 完成
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0] is payload
    assert received[0].reactions is not None
    assert received[0].reactions[0].emoji == "🔥"


async def test_vm_passes_through_none_reactions() -> None:
    """v1.7.4:reactions=None(仅 views 变化)→ 仍 emit signal,下游决定怎么用。"""
    bus = EventBus()
    vm = _make_vm(bus)
    received: list[MessageInteractionsChanged] = []
    vm.message_interactions_changed.connect(lambda e: received.append(e))

    payload = MessageInteractionsChanged(channel_id=100, telegram_msg_id=42, views=99)
    await bus.publish(payload)
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0].views == 99
    assert received[0].reactions is None


async def test_vm_ignores_non_matching_event() -> None:
    """v1.7.4:不是 `MessageInteractionsChanged` 的事件 → 静默,不 emit。"""
    from datetime import UTC, datetime

    from tgmonitor.core.dto import MessageDTO
    from tgmonitor.core.events import MessageReceived

    bus = EventBus()
    vm = _make_vm(bus)
    received: list = []
    vm.message_interactions_changed.connect(lambda e: received.append(e))

    # 发个 MessageReceived — bus 内部按类型分发,MessageInteractionsChanged handler 不应收到
    await bus.publish(
        MessageReceived(
            message=MessageDTO(
                id=0,
                channel_id=1,
                telegram_msg_id=1,
                date=datetime.now(UTC),
                text="",
            )
        )
    )
    await asyncio.sleep(0.05)
    assert received == []
