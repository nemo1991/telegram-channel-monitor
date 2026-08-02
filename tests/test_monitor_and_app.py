"""Monitor + AppService + EventBus 端到端(无网络)。"""
from __future__ import annotations

import asyncio

import pytest

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
    state, _ = await app.submit_phone("+10000000000")
    assert state == "code_required"
    state, _ = await app.submit_code("12345")
    assert state == "ready"


async def test_app_login_without_credentials_fails(tmp_path):
    """未配置凭据时,submit_phone() 应返回 ('error', ...) 而不是崩溃。"""
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, Settings
    from tgmonitor.core.events import ErrorOccurred, EventBus
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
    state, detail = await app.submit_phone("+10000000000")
    assert state == "error"
    assert detail is not None and "API_ID" in detail
    # 兼容旧断言
    assert errs and "API_ID" in errs[0].message


async def test_app_subscribe_unsubscribe(app, storage, bus):
    from tgmonitor.core.dto import ChannelDTO

    ch = ChannelDTO(id=42, title="x")
    await app.subscribe_channel(ch)
    # 2026-07-31:删 `_subscribed` 后,真理在 `list_subscribed_channels()`
    # (走 storage)。`storage` fixture 是 InMemoryRepository 直接路径。
    assert 42 in {c.id for c in await storage.list_subscribed_channels()}
    assert (await app.list_subscribed_channels())[0].id == 42
    await app.unsubscribe_channel(42)
    assert 42 not in {c.id for c in await storage.list_subscribed_channels()}
    assert (await app.list_subscribed_channels()) == []


# ============================================================
# 2026-07-31 SUBSCRIBED_DRIFT_ANALYSIS 报告(#A/#B/#C)回归保护
# ============================================================


async def test_unsubscribe_storage_failure_raises_no_false_success(app, bus):
    """#A 回归:storage 抛错时 unsubscribe_channel 必须 raise,不让 UI 误判
    "退订成功"。

    之前实现:`log.exception` 吞掉异常后仍 emit `ChannelUnsubscribed` —
    UI 把频道从「已监听」栏移除,但 storage.is_subscribed 还在,
    下次 reload 频道被恢复 → 静默失败。
    """
    from tgmonitor.core.dto import ChannelDTO

    ch = ChannelDTO(id=99, title="x")
    await app.subscribe_channel(ch)

    # Replace storage.set_channel_subscribed with a failing version
    boom = RuntimeError("simulated storage failure")

    async def _fail(*_a, **_kw):
        raise boom

    # monkey-patch the real method on the actual storage instance
    real = app.storage.set_channel_subscribed
    app.storage.set_channel_subscribed = _fail  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="simulated storage failure"):
            await app.unsubscribe_channel(99)
    finally:
        app.storage.set_channel_subscribed = real  # type: ignore[method-assign]

    # storage 应该仍 mark 99 = subscribed — unsubscribe 没真正生效
    assert 99 in {c.id for c in await app.list_subscribed_channels()}


async def test_unsubscribe_failure_does_not_publish_event(app, bus):
    """#A 配套:storage 失败时不应 publish ChannelUnsubscribed,
    避免 UI 视觉上误以为成功。
    """
    from tgmonitor.core.dto import ChannelDTO
    from tgmonitor.core.events import ChannelUnsubscribed

    ch = ChannelDTO(id=100, title="x")
    await app.subscribe_channel(ch)

    captured: list[int] = []
    bus.subscribe(ChannelUnsubscribed, lambda e: captured.append(e.channel_id))

    async def _fail(*_a, **_kw):
        raise RuntimeError("nope")

    real = app.storage.set_channel_subscribed
    app.storage.set_channel_subscribed = _fail  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            await app.unsubscribe_channel(100)
    finally:
        app.storage.set_channel_subscribed = real  # type: ignore[method-assign]

    assert captured == [], (
        f"storage 失败不应 publish ChannelUnsubscribed;got {captured}"
    )


async def test_list_messages_falls_back_to_storage_truth(app, storage):
    """#B 回归:`list_messages(channel_ids=None)` 走 storage 的
    `list_subscribed_channels()` 真理,不用任何 in-memory cache。

    之前实现:cache `self._subscribed` fallback → 与 storage 漂移时返回错集合。
    """
    from datetime import UTC, datetime

    from tgmonitor.core.dto import ChannelDTO, MessageDTO

    # 模拟 cache 已 drift 但 storage 是真理:手动插一条消息到 storage,
    # 同时确认 storage.list_subscribed_channels 包含该频道
    ch = ChannelDTO(id=200, title="c200", is_subscribed=True)
    await storage.upsert_channel(ch)
    await storage.save_message(MessageDTO(
        id=0, channel_id=200, telegram_msg_id=1, text="from-sub",
        date=datetime(2026, 7, 15, 13, 0, tzinfo=UTC),
    ))

    msgs = await app.list_messages(channel_ids=None)
    assert any(m.text == "from-sub" for m in msgs), (
        f"list_messages(None) 应从 storage 真理里拉消息;got {[m.text for m in msgs]}"
    )


async def test_no_subscribed_attribute_remains(app) -> None:
    """`_subscribed` 字段整体删除后不应再有 — 任何代码层回滚会立刻被本测试
    抓住。"""
    assert not hasattr(app, "_subscribed"), (
        "AppService._subscribed 字段已 2026-07-31 删除;"
        "若新增回该字段请先读 SUBSCRIBED_DRIFT_ANALYSIS.md"
    )
