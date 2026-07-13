"""Monitor + AppService + EventBus 端到端(无网络)。"""
from __future__ import annotations

import asyncio

from tests.conftest import make_message


async def test_monitor_receives_and_dedupes(monitor, storage, client, bus):
    monitor.set_whitelist([100])
    await monitor.start()
    try:
        # 发 3 条同 id + 1 条不同 id + 1 条不在白名单
        for _ in range(3):
            await client.simulate_incoming(make_message(channel_id=100, msg_id=1, text="dup"))
        await client.simulate_incoming(make_message(channel_id=100, msg_id=2, text="new"))
        await client.simulate_incoming(make_message(channel_id=999, msg_id=1, text="ignored"))
        # 给 monitor 一点点处理时间
        await asyncio.sleep(0.2)
        assert await storage.count_messages(100) == 2
        # 不在白名单的频道不应落库
        assert await storage.count_messages(999) == 0
    finally:
        await monitor.stop()


async def test_message_received_event_published(monitor, client, bus):
    seen: list = []
    bus.subscribe(__import__("tgmonitor.core.events", fromlist=["MessageReceived"]).MessageReceived,
                  lambda e: seen.append(e) or _noop())
    monitor.set_whitelist([100])
    await monitor.start()
    try:
        await client.simulate_incoming(make_message(channel_id=100, msg_id=1, text="evt"))
        await asyncio.sleep(0.2)
        assert any(getattr(e, "message", None) and e.message.text == "evt" for e in seen)
    finally:
        await monitor.stop()


def _noop() -> None:
    return None


async def test_app_login_state_machine(app, client):
    state = await app.login("+10000000000")
    assert state == "code_required"
    state = await app.submit_code("12345")
    assert state == "ready"


async def test_app_login_without_credentials_fails(tmp_path):
    """未配置凭据时,login() 应返回 'error' 而不是崩溃。"""
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, Settings
    from tgmonitor.core.events import EventBus, ErrorOccurred
    from tgmonitor.core.telegram.fake_client import FakeTelegramClient

    s = Settings(  # type: ignore[call-arg]
        # 故意留空
        api_id=0, api_hash="", phone="",
        db_backend=DBBackend.JSONL, db_root=tmp_path / "m",
        objectstore_backend=ObjectStoreBackend.LOCAL, objectstore_root=tmp_path / "o",
        media_policy=MediaPolicy.METADATA,
    )
    bus = EventBus()
    errs: list[ErrorOccurred] = []
    bus.subscribe(ErrorOccurred, lambda e: errs.append(e))
    from tests.conftest import InMemoryRepository
    from tgmonitor.core.objectstore.local_store import LocalObjectStore
    app = AppService(
        bus, FakeTelegramClient(),
        InMemoryRepository(),
        LocalObjectStore(root=tmp_path / "o"),
        s,
    )
    state = await app.login("+10000000000")
    assert state == "error"
    assert errs and "API_ID" in errs[0].message


async def test_app_subscribe_unsubscribe(app, storage, bus):
    from tgmonitor.core.dto import ChannelDTO

    ch = ChannelDTO(id=42, title="x")
    await app.subscribe_channel(ch)
    assert 42 in app._subscribed
    assert (await app.list_subscribed_channels())[0].id == 42
    await app.unsubscribe_channel(42)
    assert 42 not in app._subscribed
