"""ChannelSyncService 单元测试 — 用户多选频道触发的全量同步。

覆盖关键回归点:
- 元数据 + 历史消息都正确写入 storage
- 续拉(resume)从 max_msg_id 之后开始
- 取消信号立刻退出
- TelegramRateLimitError 退避后继续
- 进度事件 ChannelSyncProgress / ChannelSyncDone 都发
- save_message 幂等(重复 sync 不引入重复消息)
- 选单频道 / 多频道 路径
"""
from __future__ import annotations

from datetime import datetime

from tests.conftest import InMemoryRepository
from tgmonitor.core.channel_sync import ChannelSyncService
from tgmonitor.core.dto import (
    ChannelDTO,
    MessageDTO,
    SyncOptions,
    SyncResult,
)
from tgmonitor.core.events import EventBus
from tgmonitor.core.telegram.fake_client import FakeTelegramClient

# ---- helpers ----

async def _make_app(
    bus: EventBus, fake: FakeTelegramClient, storage: InMemoryRepository
) -> ChannelSyncService:
    return ChannelSyncService(bus, fake, storage)


def _capture_events(bus: EventBus) -> dict:
    captured: dict = {
        "progress": [],
        "done": [],
    }
    from tgmonitor.core.events import ChannelSyncDone, ChannelSyncProgress

    async def _on_p(e):
        captured["progress"].append(e)

    async def _on_d(e):
        captured["done"].append(e)

    bus.subscribe(ChannelSyncProgress, _on_p)
    bus.subscribe(ChannelSyncDone, _on_d)
    return captured


# ============================================================
# 元数据 + 历史消息落库
# ============================================================


async def test_sync_metadata_and_history(bus, client, storage, settings):
    """最简:一个频道,元数据 + 100 条历史消息全拉并落库。"""
    svc = await _make_app(bus, client, storage)
    # 注入历史:max_id=100, count=100
    client.set_history(100, max_id=100, count=100)
    # 注入元数据
    client.set_metadata(ChannelDTO(
        id=100, title="Telegram News",
        username="tnews", kind="channel", member_count=12345,
    ))
    captured = _capture_events(bus)

    options = SyncOptions(
        include_metadata=True, include_history=True,
        chat_delay_ms=0, page_delay_ms=0,  # 加速
    )
    result = await svc.sync_channels([100], options)

    # 元数据落库
    ch = await storage.get_channel(100)
    assert ch is not None
    assert ch.title == "Telegram News"
    assert ch.username == "tnews"
    assert ch.member_count == 12345
    assert ch.last_synced_at is not None
    # 历史消息落库
    msgs = await storage.list_messages([100])
    assert len(msgs) == 100
    # 进度事件
    assert any(e.stage == "metadata" for e in captured["progress"])
    assert any(e.stage == "history" for e in captured["progress"])
    assert any(e.stage == "done" for e in captured["progress"])
    # 完成事件
    assert len(captured["done"]) == 1
    done_evt = captured["done"][0]
    assert isinstance(done_evt.result, SyncResult)
    assert done_evt.result.total_messages_added == 100
    assert not done_evt.result.cancelled
    # per_channel
    assert 100 in result.per_channel
    assert result.per_channel[100].metadata_updated
    assert result.per_channel[100].messages_added == 100


async def test_sync_metadata_only(bus, client, storage, settings):
    """只选元数据时不应拉历史。"""
    svc = await _make_app(bus, client, storage)
    client.set_history(100, max_id=100, count=100)
    client.set_metadata(ChannelDTO(id=100, title="X", kind="channel"))

    options = SyncOptions(
        include_metadata=True, include_history=False,
        chat_delay_ms=0, page_delay_ms=0,
    )
    await svc.sync_channels([100], options)
    msgs = await storage.list_messages([100])
    assert len(msgs) == 0


async def test_sync_history_only(bus, client, storage, settings):
    """只选历史时不应写 last_synced_at。"""
    svc = await _make_app(bus, client, storage)
    client.set_history(100, max_id=100, count=50)
    options = SyncOptions(
        include_metadata=False, include_history=True,
        chat_delay_ms=0, page_delay_ms=0,
    )
    await svc.sync_channels([100], options)
    ch = await storage.get_channel(100)
    assert ch is not None
    # 元数据没刷 — last_synced_at 应是 None
    assert ch.last_synced_at is None
    msgs = await storage.list_messages([100])
    assert len(msgs) == 50


# ============================================================
# 续拉(resume) — 不重复拉已有消息
# ============================================================


async def test_resume_from_saved_skips_existing(bus, client, storage, settings):
    """已有 max_id=50,client 那边 max=100,只应拉 51-100(50 条)。"""
    # 先存 50 条到 storage
    for mid in range(1, 51):
        await storage.save_message(MessageDTO(
            id=0, channel_id=100, telegram_msg_id=mid,
            text=f"old-{mid}", date=datetime(2026, 1, 1),
        ))
    svc = await _make_app(bus, client, storage)
    client.set_history(100, max_id=100, count=100)
    options = SyncOptions(
        include_metadata=False, include_history=True,
        chat_delay_ms=0, page_delay_ms=0, resume_from_saved=True,
    )
    captured = _capture_events(bus)
    await svc.sync_channels([100], options)
    msgs = await storage.list_messages([100])
    # 原有 50 + 新拉 50 = 100
    assert len(msgs) == 100
    # total_messages_added 是"新落库",不含已存在的
    assert captured["done"][0].result.total_messages_added == 50


async def test_resume_disabled_re_pulls_all(bus, client, storage, settings):
    """resume_from_saved=False → 即使 storage 有 max_id,也从头拉。
    save_message 幂等 → 总条数不变,total_messages_added = 0。"""
    for mid in range(1, 51):
        await storage.save_message(MessageDTO(
            id=0, channel_id=100, telegram_msg_id=mid,
            text=f"old-{mid}", date=datetime(2026, 1, 1),
        ))
    svc = await _make_app(bus, client, storage)
    client.set_history(100, max_id=100, count=100)
    options = SyncOptions(
        include_metadata=False, include_history=True,
        chat_delay_ms=0, page_delay_ms=0, resume_from_saved=False,
    )
    captured = _capture_events(bus)
    await svc.sync_channels([100], options)
    msgs = await storage.list_messages([100])
    assert len(msgs) == 100  # 仍然 100(幂等)
    # resume=False + storage 已有 50 → 重拉全 100,新落 50(原 1-50 已存,51-100 是新的)
    assert captured["done"][0].result.total_messages_added == 50


# ============================================================
# 取消
# ============================================================


async def test_cancel_stops_sync_immediately(bus, client, storage, settings):
    """cancel() 之后,正在跑的 sync 应立刻退出。"""
    svc = await _make_app(bus, client, storage)
    client.set_history(100, max_id=100, count=100)
    # 设个慢 delay(50ms 每条),让取消信号有机会插进来
    options = SyncOptions(
        include_metadata=False, include_history=True,
        chat_delay_ms=50, page_delay_ms=0,
    )
    import asyncio

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.05)
        svc.cancel()
    asyncio.create_task(_cancel_soon())
    result = await svc.sync_channels([100], options)
    # 应被取消
    assert result.cancelled
    # per_channel 应包含这个 id(error=cancelled)
    assert 100 in result.per_channel
    assert result.per_channel[100].error == "cancelled"


# ============================================================
# TelegramRateLimitError 退避
# ============================================================


async def test_rate_limit_triggers_backoff_and_continues(bus, client, storage, settings):
    """iter_chat_history 第 30 条抛 429 → 退避后整轮继续;最终 per_channel 含 error。"""
    svc = await _make_app(bus, client, storage)
    client.set_history(100, max_id=100, count=100)
    client.inject_rate_limit_after(30)  # 第 30+1 条前抛
    options = SyncOptions(
        include_metadata=False, include_history=True,
        chat_delay_ms=0, page_delay_ms=0,
    )
    captured = _capture_events(bus)
    result = await svc.sync_channels([100], options)
    # rate_limited 应被记录
    assert result.rate_limited_seconds is not None
    assert result.rate_limited_seconds > 0
    # 该频道被标 rate_limited
    assert result.per_channel[100].rate_limited
    # backoff 事件发了
    assert any(e.stage == "backoff" for e in captured["progress"])
    # 但 sync 整体没取消(单频道退避,继续下一个)
    assert not result.cancelled


async def test_rate_limit_during_metadata_skips_history(bus, client, storage, settings):
    """元数据阶段就撞 429 → 不应继续拉历史(error 早于 history 阶段)。"""
    svc = await _make_app(bus, client, storage)

    # 用 wrapper 替换 get_channel_metadata,只为 100 抛
    from tgmonitor.core.telegram.tdlib_client import TelegramRateLimitError
    orig = client.get_channel_metadata

    async def _selective(cid: int):
        if cid == 100:
            raise TelegramRateLimitError(0.01)  # 10ms
        return await orig(cid)
    client.get_channel_metadata = _selective  # type: ignore[assignment]

    # 准备 history(不应被触达)
    client.set_history(100, max_id=100, count=100)
    options = SyncOptions(
        include_metadata=True, include_history=True,
        chat_delay_ms=0, page_delay_ms=0,
    )
    result = await svc.sync_channels([100], options)
    assert result.per_channel[100].rate_limited
    # 历史不应被拉(从未走到 history 阶段)
    msgs = await storage.list_messages([100])
    assert len(msgs) == 0


# ============================================================
# 多频道串行
# ============================================================


async def test_sync_multiple_channels_serial(bus, client, storage, settings):
    """两频道 — 应串行处理,都进 per_channel。"""
    svc = await _make_app(bus, client, storage)
    client.set_metadata(ChannelDTO(id=1, title="A", kind="channel"))
    client.set_metadata(ChannelDTO(id=2, title="B", kind="channel"))
    client.set_history(1, max_id=10, count=10)
    client.set_history(2, max_id=20, count=20)
    options = SyncOptions(
        include_metadata=True, include_history=True,
        chat_delay_ms=0, page_delay_ms=0,
    )
    result = await svc.sync_channels([1, 2], options)
    assert 1 in result.per_channel and 2 in result.per_channel
    msgs_1 = await storage.list_messages([1])
    msgs_2 = await storage.list_messages([2])
    assert len(msgs_1) == 10
    assert len(msgs_2) == 20


async def test_sync_continues_after_one_channel_fails(
    bus, client, storage, settings
):
    """一频道 metadata 失败,不应阻塞下一个频道。"""
    svc = await _make_app(bus, client, storage)
    from tgmonitor.core.telegram.tdlib_client import TelegramRateLimitError

    # 用 wrapper 替 client.get_channel_metadata,只为 id=1 抛
    orig = client.get_channel_metadata

    async def _selective(cid: int):
        if cid == 1:
            raise TelegramRateLimitError(0.01)  # 10ms 退避
        return await orig(cid)
    client.get_channel_metadata = _selective  # type: ignore[assignment]

    # 2 号频道 metadata 正常(走 orig)
    client.set_metadata(ChannelDTO(id=2, title="OK", kind="channel"))
    client.set_history(2, max_id=5, count=5)
    options = SyncOptions(
        include_metadata=True, include_history=True,
        chat_delay_ms=0, page_delay_ms=0,
    )
    result = await svc.sync_channels([1, 2], options)
    # 1 标记 rate_limited,2 成功
    assert result.per_channel[1].rate_limited
    assert result.per_channel[2].metadata_updated
    # 2 落库
    msgs = await storage.list_messages([2])
    assert len(msgs) == 5


# ============================================================
# 元数据语义:upsert_channel_metadata 不改 subscribed
# ============================================================


async def test_metadata_sync_does_not_change_subscribed(
    bus, client, storage, settings
):
    """ChannelDTO.from_settings sync 时传入 is_subscribed=False,
    但 upsert_channel_metadata 必须保留旧 is_subscribed 不动。
    """
    # 用户订阅了 100 频道
    await storage.set_channel_subscribed(100, True)
    # 模拟 sync 拉到的 DTO(sync 路径不传 is_subscribed,默认 False)
    dto = ChannelDTO(id=100, title="Updated", kind="channel")
    # 直接调 storage 方法
    await storage.upsert_channel_metadata(dto)
    # 已订阅状态应保留 True
    ch = await storage.get_channel(100)
    assert ch is not None
    assert ch.is_subscribed is True
    assert ch.title == "Updated"


# ============================================================
# TelegramRateLimitError 翻译辅助
# ============================================================


def test_translate_rate_limit_429():
    from tgmonitor.core.telegram.tdlib_client import (
        TdlibTelegramClient,
        TelegramRateLimitError,
    )

    class _FakeError:
        code = 429
        retry_after = 12
    got = TdlibTelegramClient._translate_rate_limit(_FakeError())  # type: ignore[arg-type]
    assert isinstance(got, TelegramRateLimitError)
    assert got.retry_after_seconds == 12


def test_translate_rate_limit_flood_wait_text():
    from tgmonitor.core.telegram.tdlib_client import (
        TdlibTelegramClient,
        TelegramRateLimitError,
    )

    class _FakeError:
        code = 0
        message = "FLOOD_WAIT_42 something"
    got = TdlibTelegramClient._translate_rate_limit(_FakeError())  # type: ignore[arg-type]
    assert isinstance(got, TelegramRateLimitError)
    assert got.retry_after_seconds == 42


def test_translate_rate_limit_returns_none_for_unrelated():
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient

    class _FakeError:
        code = 400
        message = "Something else"
    got = TdlibTelegramClient._translate_rate_limit(_FakeError())  # type: ignore[arg-type]
    assert got is None
