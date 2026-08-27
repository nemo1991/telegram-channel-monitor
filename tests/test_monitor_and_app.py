"""Monitor + AppService + EventBus 端到端(无网络)。"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import AsyncIterator

import pytest

from tests.conftest import make_message
from tgmonitor.core.dto import MediaDTO, MessageDTO, ReactionDTO
from tgmonitor.core.events import MessageInteractionsChanged
from tgmonitor.core.telegram.fake_client import FakeTelegramClient


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


# ============================================================
# 周期补拉(backfill)— 断线 / 重启期间 updateNewMessage 不重放的兜底
# ============================================================


class _BackfillClient(FakeTelegramClient):
    """`iter_chat_history` 按真实 TDLib 语义 yield:最新在前(向旧递减)。

    conftest 的 `FakeTelegramClient` 为 channel_sync 的 resume 语义测试按
    升序 yield,与真实 TDLib 相反;补拉逻辑依赖"最新在前",这里给忠实版本。
    """

    def __init__(self, history: dict[int, list[int]]) -> None:
        """`history`:channel_id → telegram_msg_id 列表(最新在前)。

        补拉只应在登录成功(ready)后执行,故默认置 ready。
        """
        super().__init__()
        self._state = "ready"
        self._history = history

    async def iter_chat_history(  # type: ignore[override]
        self,
        channel_id: int,
        *,
        before_msg_id: int = 0,
        limit: int = 100,
    ) -> AsyncIterator[MessageDTO]:
        for mid in self._history.get(channel_id, []):
            await asyncio.sleep(0)  # 让出 loop,模仿网络
            yield make_message(channel_id=channel_id, msg_id=mid, text=f"backfill-{mid}")


async def test_backfill_fills_gap_and_skips_known(bus, storage, objectstore, settings):
    """库里已有 id=100;历史最新在前 [104..100] → 只补 104-101,100 不重复 emit。"""
    from tgmonitor.core.events import MessageReceived
    from tgmonitor.core.monitor.service import MonitorService

    client = _BackfillClient({100: [104, 103, 102, 101, 100]})
    await storage.save_message(make_message(channel_id=100, msg_id=100, text="known"))
    mon = MonitorService(bus, client, storage, objectstore, settings)
    mon.set_whitelist([100])
    seen: list[MessageReceived] = []
    bus.subscribe(MessageReceived, lambda e: seen.append(e))
    await mon._backfill_all()  # 不 start 也直接可用(不依赖实时流)
    msgs = await storage.list_messages([100])
    assert {m.telegram_msg_id for m in msgs} == {100, 101, 102, 103, 104}
    # 只对"新"消息 emit,已落库的 100 不重复通知
    assert {e.message.telegram_msg_id for e in seen} == {101, 102, 103, 104}


async def test_backfill_loop_runs_periodically_and_stops(bus, storage, objectstore, settings):
    """周期补拉:interval 调小后,start 后消息自动入库;stop 干净退出不再补。"""
    from tgmonitor.core.monitor.service import MonitorService

    client = _BackfillClient({100: [5, 4, 3, 2, 1]})
    mon = MonitorService(bus, client, storage, objectstore, settings)
    mon.set_whitelist([100])
    mon._BACKFILL_INTERVAL = 0.02  # type: ignore[assignment]
    await mon.start()
    try:
        await asyncio.sleep(0.12)
        # 首轮全补;后续轮锚点=5,第一条第 5 条 <= 5 → break,不重复
        assert await storage.count_messages(100) == 5
    finally:
        await mon.stop()
    count_after_stop = await storage.count_messages(100)
    assert count_after_stop == 5


async def test_backfill_silent_when_client_closing(bus, storage, objectstore, settings):
    """close() 中补拉 → 静默返回(不抛、不落库、不打 traceback)。"""
    from tgmonitor.core.monitor.service import MonitorService
    from tgmonitor.core.telegram.tdlib_errors import ClientClosingError

    class _ClosingClient(_BackfillClient):
        async def iter_chat_history(  # type: ignore[override]
            self, channel_id: int, *, before_msg_id: int = 0, limit: int = 100
        ) -> AsyncIterator[MessageDTO]:
            raise ClientClosingError()
            yield  # noqa: B018  — 让函数成为 async generator(否则 async for 拿不到异常)

    mon = MonitorService(bus, _ClosingClient({100: [1]}), storage, objectstore, settings)
    mon.set_whitelist([100])
    await mon._backfill_all()
    assert await storage.count_messages(100) == 0


async def test_backfill_unanchored_capped_to_max_page(bus, storage, objectstore, settings):
    """无锚点频道:只预热 `_BACKFILL_MAX_PAGE` 条,不把整段历史拉完。

    回归:`max_id=0` 时 `<= 0` 永假,旧实现每轮把整段历史翻完,几万条会撞
    flood wait / 制造长空窗。
    """
    from tgmonitor.core.monitor.service import MonitorService

    client = _BackfillClient({100: [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]})
    mon = MonitorService(bus, client, storage, objectstore, settings)
    mon._BACKFILL_MAX_PAGE = 3  # type: ignore[assignment]
    mon.set_whitelist([100])
    await mon._backfill_all()
    msgs = await storage.list_messages([100])
    assert {m.telegram_msg_id for m in msgs} == {10, 9, 8}


async def test_backfill_skipped_when_not_ready(bus, storage, objectstore, settings):
    """未登录(非 ready)时补拉直接跳过:不拉取频道信息、不落库、不打 traceback。

    回归:app 启动时 monitor 先于登录完成启动,旧实现每轮都调 getChatHistory
    → TDLib 抛 "Client not started" → 每 30s 刷一轮 ERROR。登录成功后
    state 变 ready,下一轮补拉自动恢复。
    """
    from tgmonitor.core.monitor.service import MonitorService

    client = _BackfillClient({100: [10, 9, 8]})
    client._state = "phone_required"  # 未登录
    mon = MonitorService(bus, client, storage, objectstore, settings)
    mon.set_whitelist([100])
    await mon._backfill_all()
    assert await storage.count_messages(100) == 0


# ============================================================
# iter_chat_history 预热(getChat)— [400] Chat not found 修复
# ============================================================


class _ChatHistoryClient:
    """ChannelsApi 的假父 client:记录请求、按 @type 分发。

    只实现 `iter_chat_history` 用到的两个入口(`getChat` / `getChatHistory`)
    与 `_check_alive()`;其它请求直接 AssertionError。
    """

    def __init__(self, *, chat_available: bool = True, history: list | None = None) -> None:
        """`chat_available=False` 模拟 getChat 返回 Chat not found。"""
        self.chat_available = chat_available
        self.history = history or []
        self.calls: list[str] = []

    def _check_alive(self) -> None:
        return None

    async def request(self, payload: dict) -> object:
        self.calls.append(payload["@type"])
        if payload["@type"] == "getChat":
            if not self.chat_available:
                from tdlib_json.errors import TdlibError

                raise TdlibError(400, "Chat not found")
            return type("Chat", (), {"id": payload["chat_id"], "title": "t"})()
        if payload["@type"] == "getChatHistory":
            return type("History", (), {"messages": self.history})()
        raise AssertionError(f"unexpected request: {payload}")


async def test_iter_chat_history_unavailable_chat_raises():
    """getChat 预热失败(Chat not found)→ 抛 ChatUnavailableError,不再每页 400。"""
    from tgmonitor.core.telegram.tdlib_channels import ChannelsApi, ChatUnavailableError

    client = _ChatHistoryClient(chat_available=False)
    api = ChannelsApi(client)
    with pytest.raises(ChatUnavailableError) as ei:
        [m async for m in api.iter_chat_history(100)]
    assert ei.value.channel_id == 100
    assert client.calls == ["getChat"]  # 预热先行,没发 getChatHistory


async def test_iter_chat_history_warms_up_chat_then_yields():
    """预热 getChat 成功后照常分页 yield MessageDTO(旧行为不回归)。"""
    from tgmonitor.core.telegram.tdlib_channels import ChannelsApi

    raw = type("Message", (), {
        "id": 5, "chat_id": 100, "date": 0, "author_signature": None,
        "content": type("MessageText", (), {"text": "hi"})(),
        "views": None, "forwards": None, "edit_date": 0,
    })()
    client = _ChatHistoryClient(history=[raw])
    api = ChannelsApi(client)
    msgs = [m async for m in api.iter_chat_history(100)]
    assert client.calls[0] == "getChat"
    assert "getChatHistory" in client.calls
    assert len(msgs) == 1
    assert msgs[0].channel_id == 100 and msgs[0].telegram_msg_id == 5


async def test_backfill_unavailable_channel_warns_once_and_continues(
    bus, storage, objectstore, settings, caplog
):
    """频道不可访问(ChatUnavailableError)→ 只 warning 一次、跳过、不刷 traceback;
    同轮其它频道照常补拉;恢复可用后下一轮自动继续。
    """
    import logging

    from tgmonitor.core.monitor.service import MonitorService
    from tgmonitor.core.telegram.tdlib_channels import ChatUnavailableError

    class _UnavailableClient(_BackfillClient):
        """history 里标记为 unavailable 的频道抛 ChatUnavailableError。"""

        def __init__(self, history: dict[int, list[int]]) -> None:
            super().__init__(history)
            self.unavailable: set[int] = set()

        async def iter_chat_history(  # type: ignore[override]
            self, channel_id: int, *, before_msg_id: int = 0, limit: int = 100
        ) -> AsyncIterator[MessageDTO]:
            if channel_id in self.unavailable:
                raise ChatUnavailableError(channel_id, reason="Chat not found")
            async for m in super().iter_chat_history(
                channel_id, before_msg_id=before_msg_id, limit=limit
            ):
                yield m

    client = _UnavailableClient({100: [5, 4], 101: [3, 2, 1]})
    client.unavailable = {100}
    mon = MonitorService(bus, client, storage, objectstore, settings)
    mon.set_whitelist([100, 101])

    with caplog.at_level(logging.WARNING, logger="tgmonitor.core.monitor.service"):
        await mon._backfill_all()
    assert await storage.count_messages(100) == 0  # 不可用频道不落库
    assert "unavailable" in caplog.text  # warning 而非 traceback
    assert "Traceback" not in caplog.text
    assert await storage.count_messages(101) == 3  # 同轮其它频道照常补

    # 再跑一轮:warning 只打一次,不重复刷
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tgmonitor.core.monitor.service"):
        await mon._backfill_all()
    assert "unavailable" not in caplog.text

    # 恢复可用后:下一轮照常补,并清掉 suppression 标记
    client.unavailable = set()
    await mon._backfill_all()
    assert await storage.count_messages(100) == 2


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


# ============================================================
# 心跳 debug 日志(排查"一段时间不监听"的诊断信号)
# ============================================================


async def test_monitor_heartbeat_logs_when_stream_idle(monitor, caplog) -> None:
    """流静默超过 `_HEARTBEAT_INTERVAL` 时主循环打心跳 INFO 日志。

    这是排查"空窗"的第一手信号:有 `monitor heartbeat` 就证明实时通道还
    活着,只是没新消息;没有则说明流死了(配合自愈重启的 ERROR 看)。
    心跳是 INFO 级别 — 不设 TG_LOG_LEVEL 也默认可见。
    """
    monitor._HEARTBEAT_INTERVAL = 0.02  # type: ignore[assignment]
    with caplog.at_level("INFO", logger="tgmonitor.core.monitor.service"):
        await monitor.start()
        await asyncio.sleep(0.08)
        await monitor.stop()
    assert any(
        "monitor heartbeat" in r.message and "stream alive" in r.message
        for r in caplog.records
    ), f"心跳日志缺失;records={[r.message for r in caplog.records]}"
    assert all(
        r.levelname == "INFO" for r in caplog.records if "monitor heartbeat" in r.message
    ), f"心跳应为 INFO 级别;records={[r.message for r in caplog.records]}"


async def test_monitor_logs_update_received_and_stored(monitor, client, caplog) -> None:
    """收到实时包打 `update received` DEBUG,落库后打 `stored message`(带计数)。

    能区分「流有包进来」与「真落库成功」;handled 计数逐条递增,方便确认
    处理在推进而不是卡在某条消息上。
    """
    monitor.set_whitelist([100])
    await monitor.start()
    try:
        with caplog.at_level("DEBUG", logger="tgmonitor.core.monitor.service"):
            await client.simulate_incoming(make_message(channel_id=100, msg_id=7, text="hb"))
            await asyncio.sleep(0.05)
    finally:
        await monitor.stop()
    messages = [r.message for r in caplog.records]
    assert any("monitor update received" in m and "msg_id=7" in m for m in messages), messages
    assert any("monitor stored message" in m and "handled=1" in m for m in messages), messages


async def test_monitor_heartbeat_logs_when_stream_active(
    monitor, client, caplog
) -> None:
    """活跃频道(消息不断)下心跳也周期打 — 不是只在静默时。

    用户反馈「启动 10 分钟没看到心跳日志」:若频道 30s 内一直有新消息,
    旧实现只在静默超时打心跳,活跃时用户永远见不到 heartbeat 行,只见
    `update received`。这里验证有消息时同样按周期出现 heartbeat。
    """
    monitor._HEARTBEAT_INTERVAL = 0.03  # type: ignore[assignment]
    monitor.set_whitelist([100])
    await monitor.start()
    try:
        # DEBUG 才能捕获 update received(仍是 DEBUG);heartbeat 是 INFO,
        # DEBUG 级别下同样捕获,且下方断言其 levelname 为 INFO。
        with caplog.at_level("DEBUG", logger="tgmonitor.core.monitor.service"):
            # 每隔 0.01s 推一条 → 流一直活跃,期间应出现 heartbeat(周期节流)
            for i in range(8):
                await client.simulate_incoming(
                    make_message(channel_id=100, msg_id=100 + i, text=f"hb-{i}")
                )
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
    finally:
        await monitor.stop()
    messages = [r.message for r in caplog.records]
    assert any(
        "monitor heartbeat" in m and "stream alive" in m for m in messages
    ), f"活跃流下心跳缺失;records={messages}"
    assert any("monitor update received" in m for m in messages), messages
    assert all(
        r.levelname == "INFO" for r in caplog.records if "monitor heartbeat" in r.message
    ), f"心跳应为 INFO 级别;records={messages}"


# ============================================================
# 异步下载队列(FULL 策略:先落库 → 后台下载 → 回写状态 + MediaDownloaded)
# ============================================================


def _media_with_file_id(file_id: str, **overrides) -> MediaDTO:
    """构造带 telegram_file_id 的 MediaDTO(下载队列用)。"""
    from tgmonitor.core.dto import MediaType

    return MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name="pic.jpg",
        file_size=11,
        telegram_file_id=file_id,
        **overrides,
    )


class _SlowDownloadClient(FakeTelegramClient):
    """`download_file` 睡 0.2s 再返回 — 模拟大文件下载耗时。

    让「先落库 DOWNLOADING → 后台下载 → 回写 DONE」的时序可确定性观察,
    而不是在 0.05s 的 sleep 里被秒完成下载的假 client 竞态掉。
    """

    async def download_file(self, file_id: str) -> bytes | None:
        await asyncio.sleep(0.2)
        return await super().download_file(file_id)


async def test_full_policy_downloads_media_async(
    bus, storage, objectstore, settings
) -> None:
    """FULL 策略 + MediaDownloader:新消息先落库(DOWNLOADING)→ 后台下载 →
    回写 DONE + object_key → 发 MediaDownloaded 事件;消息不阻塞落库。

    回归:大文件下载(最长 30 分钟)不再阻塞消息落库 —— 用户立即可见
    「下载中」状态,而不是看到有记录无文件的空窗。
    """
    from tgmonitor.core.config import MediaPolicy
    from tgmonitor.core.dto import MediaDownloadStatus
    from tgmonitor.core.events import MediaDownloaded, MessageReceived
    from tgmonitor.core.monitor.service import MediaDownloader, MonitorService

    client = _SlowDownloadClient()
    client.set_download("fid-1", b"image-bytes")
    full = settings.model_copy(update={"media_policy": MediaPolicy.FULL})
    mon = MonitorService(
        bus, client, storage, objectstore, full,
        downloader=MediaDownloader(client, storage, objectstore),
    )
    mon.set_whitelist([100])
    received: list[MessageReceived] = []
    downloaded: list[MediaDownloaded] = []
    downloaded_evt = asyncio.Event()

    async def _on_received(e: MessageReceived) -> None:
        received.append(e)

    async def _on_downloaded(e: MediaDownloaded) -> None:
        downloaded.append(e)
        downloaded_evt.set()

    bus.subscribe(MessageReceived, _on_received)
    bus.subscribe(MediaDownloaded, _on_downloaded)

    msg = make_message(
        channel_id=100, msg_id=10, text="media!",
        media=[_media_with_file_id("fid-1")],
    )
    await mon.start()
    try:
        await client.simulate_incoming(msg)
        # 下载未完成(假 client 睡 0.2s)→ 此刻应已落库且 media 是 DOWNLOADING
        await asyncio.sleep(0.05)
        assert len(received) == 1, "MessageReceived 应先行发布"
        assert received[0].message.media[0].download_status == (
            MediaDownloadStatus.DOWNLOADING
        )
        stored = await storage.get_message(100, 10)
        assert stored is not None
        assert stored.media[0].download_status == MediaDownloadStatus.DOWNLOADING
        # 等 worker 完成下载并回写(超时报错)
        await asyncio.wait_for(downloaded_evt.wait(), timeout=2.0)
        assert len(downloaded) == 1
        med = downloaded[0].media
        assert med is not None
        assert med.download_status == MediaDownloadStatus.DONE
        assert med.object_key, "下载成功应回写 object_key"
        # storage 已回写 DONE
        stored = await storage.get_message(100, 10)
        assert stored is not None
        assert stored.media[0].download_status == MediaDownloadStatus.DONE
        assert stored.media[0].object_key == med.object_key
        # 对象存储真实有文件
        assert await objectstore.exists(med.object_key)
        assert await objectstore.get(med.object_key) == b"image-bytes"
    finally:
        await mon.stop()


async def test_full_policy_download_failure_marks_failed(
    bus, client, storage, objectstore, settings
) -> None:
    """下载失败 → 回写 FAILED + download_error(UI 可见原因),不阻塞消息落库。"""
    from tgmonitor.core.config import MediaPolicy
    from tgmonitor.core.dto import MediaDownloadStatus
    from tgmonitor.core.events import MediaDownloaded
    from tgmonitor.core.monitor.service import MediaDownloader, MonitorService

    full = settings.model_copy(update={"media_policy": MediaPolicy.FULL})
    mon = MonitorService(
        bus, client, storage, objectstore, full,
        downloader=MediaDownloader(client, storage, objectstore),
    )
    mon.set_whitelist([100])
    downloaded: list[MediaDownloaded] = []
    downloaded_evt = asyncio.Event()

    async def _on_downloaded(e: MediaDownloaded) -> None:
        downloaded.append(e)
        downloaded_evt.set()

    bus.subscribe(MediaDownloaded, _on_downloaded)

    client.set_download("fid-2", None)  # 注入 None = 下载失败
    msg = make_message(
        channel_id=100, msg_id=11, text="broken",
        media=[_media_with_file_id("fid-2")],
    )
    await mon.start()
    try:
        await client.simulate_incoming(msg)
        await asyncio.wait_for(downloaded_evt.wait(), timeout=2.0)
        assert len(downloaded) == 1
        med = downloaded[0].media
        assert med is not None
        assert med.download_status == MediaDownloadStatus.FAILED
        assert med.download_error, "失败应带原因"
        assert med.object_key is None
        stored = await storage.get_message(100, 11)
        assert stored is not None
        assert stored.media[0].download_status == MediaDownloadStatus.FAILED
        assert stored.media[0].download_error
    finally:
        await mon.stop()


# ============================================================
# 2026-08-24:monitor dedup + edit-event path
# ============================================================


async def test_live_monitor_re_arrival_routes_to_edit_path(
    monitor, client, bus, storage
) -> None:
    """同 id 重推(session 内)_seen_ids 命中 → 走 _handle_edited 而非 _handle。

    行为:
    - 第 1 次 push → MessageReceived(text=v1)+ save_message(v1)
    - 第 2 次 push(同 id,text=v2)→ _seen_ids 已记录 → 走 _handle_edited
      → MessageEdited + update_message(text=v2 覆盖)
    - MessageReceived 只发 1 次(编辑不发新消息事件,避免 UI 把它当新插入)
    """
    from tgmonitor.core.events import MessageEdited, MessageReceived

    monitor.set_whitelist([100])
    received: list[MessageReceived] = []
    edited: list[MessageEdited] = []
    edited_evt = asyncio.Event()

    async def _on_recv(e: MessageReceived) -> None:
        received.append(e)
    async def _on_edit(e: MessageEdited) -> None:
        edited.append(e)
        edited_evt.set()
    bus.subscribe(MessageReceived, _on_recv)
    bus.subscribe(MessageEdited, _on_edit)

    await monitor.start()
    try:
        await client.simulate_incoming(make_message(channel_id=100, msg_id=1, text="v1"))
        await asyncio.sleep(0.1)
        assert len(received) == 1
        # 第 2 次:同 id,v2 — 应走编辑路径
        await client.simulate_incoming(make_message(channel_id=100, msg_id=1, text="v2"))
        await asyncio.wait_for(edited_evt.wait(), timeout=2.0)
        # MessageReceived 仍 1 条(编辑不发新消息事件)
        assert len(received) == 1
        # MessageEdited 1 条,text 已是 v2
        assert len(edited) == 1
        assert edited[0].message is not None
        assert edited[0].message.text == "v2"
        # storage 文本被覆盖为 v2
        stored = await storage.get_message(100, 1)
        assert stored is not None and stored.text == "v2"
    finally:
        await monitor.stop()


async def test_live_monitor_silent_skip_when_message_in_storage(
    monitor, client, bus, storage
) -> None:
    """同 id 推 3 次 → 仅 1 条 MessageReceived,后 2 次都走编辑路径发 MessageEdited。

    与 test_live_monitor_re_arrival_routes_to_edit_path 互为补充:那个测 1 次
    重发,这个测连续 3 次,验证 _seen_ids 一致命中。
    """
    from tgmonitor.core.events import MessageEdited, MessageReceived

    monitor.set_whitelist([100])
    received: list[MessageReceived] = []
    edited: list[MessageEdited] = []

    async def _on_recv(e: MessageReceived) -> None:
        received.append(e)
    async def _on_edit(e: MessageEdited) -> None:
        edited.append(e)
    bus.subscribe(MessageReceived, _on_recv)
    bus.subscribe(MessageEdited, _on_edit)

    await monitor.start()
    try:
        for i in range(3):
            await client.simulate_incoming(
                make_message(channel_id=100, msg_id=1, text=f"v{i}")
            )
        await asyncio.sleep(0.2)
        # 仅 1 条 MessageReceived
        assert len(received) == 1
        # 2 条 MessageEdited
        assert len(edited) == 2
        # storage 文本被覆盖到最后一轮(v2)
        stored = await storage.get_message(100, 1)
        assert stored is not None and stored.text == "v2"
    finally:
        await monitor.stop()


async def test_full_policy_skips_download_when_storage_has_prior(
    monitor, client, bus, storage, objectstore, settings
) -> None:
    """跨消息 media 去重:msg 1 已下完,msg 2 同 file_id → 不重下,MediaDownloaded 不发。

    msg 2 落库时 media 已是 DONE + object_key 拷自 storage 优先副本。
    """
    from tgmonitor.core.config import MediaPolicy
    from tgmonitor.core.dto import MediaDownloadStatus
    from tgmonitor.core.events import MediaDownloaded, MessageReceived
    from tgmonitor.core.monitor.service import MediaDownloader, MonitorService

    client.set_download("fid-X", b"shared-bytes")
    full = settings.model_copy(update={"media_policy": MediaPolicy.FULL})
    mon = MonitorService(
        bus, client, storage, objectstore, full,
        downloader=MediaDownloader(client, storage, objectstore),
    )
    mon.set_whitelist([100])
    received: list[MessageReceived] = []
    downloaded: list[MediaDownloaded] = []
    downloaded_evt = asyncio.Event()

    async def _on_received(e: MessageReceived) -> None:
        received.append(e)

    async def _on_downloaded(e: MediaDownloaded) -> None:
        downloaded.append(e)
        downloaded_evt.set()
    bus.subscribe(MessageReceived, _on_received)
    bus.subscribe(MediaDownloaded, _on_downloaded)

    msg1 = make_message(
        channel_id=100, msg_id=10, text="first",
        media=[_media_with_file_id("fid-X")],
    )
    msg2 = make_message(
        channel_id=100, msg_id=11, text="second",
        media=[_media_with_file_id("fid-X")],
    )
    await mon.start()
    try:
        # msg 1:正常下载
        await client.simulate_incoming(msg1)
        await asyncio.wait_for(downloaded_evt.wait(), timeout=2.0)
        assert len(downloaded) == 1
        downloaded_evt.clear()
        # msg 2:同 file_id → _handle 阶段拷 storage 优先副本 → 不入下载队列
        await client.simulate_incoming(msg2)
        await asyncio.sleep(0.3)
        # 仅 msg 1 的下载事件
        assert len(downloaded) == 1, "msg 2 应命中 media dedup,不重下"
        # msg 2 落库时 media 已 DONE + object_key
        assert len(received) == 2
        msg2_received = received[1].message
        assert msg2_received is not None
        assert msg2_received.media[0].download_status == MediaDownloadStatus.DONE
        assert msg2_received.media[0].object_key, "应拷 storage 优先 object_key"
    finally:
        await mon.stop()


async def test_full_policy_dedup_cross_messages_via_storage(
    monitor, client, bus, storage, objectstore, settings
) -> None:
    """不同 file_name 但同 telegram_file_id → storage find_media_by_file_id 命中。

    与上面互补:这个测 storage skip #1(MediaDownloader.download_one 入口),
    上面测 _handle 阶段的 media dedup。两条路径一起覆盖。
    """
    from tgmonitor.core.config import MediaPolicy
    from tgmonitor.core.dto import MediaDownloadStatus
    from tgmonitor.core.events import MediaDownloaded
    from tgmonitor.core.monitor.service import MediaDownloader, MonitorService

    client.set_download("fid-Y", b"only-once")
    full = settings.model_copy(update={"media_policy": MediaPolicy.FULL})
    mon = MonitorService(
        bus, client, storage, objectstore, full,
        downloader=MediaDownloader(client, storage, objectstore),
    )
    mon.set_whitelist([100])
    downloaded: list = []
    downloaded_evt = asyncio.Event()

    async def _on_dl(e):
        downloaded.append(e)
        downloaded_evt.set()
    bus.subscribe(MediaDownloaded, _on_dl)

    msg1 = make_message(
        channel_id=100, msg_id=20, text="a",
        media=[_media_with_file_id("fid-Y")],
    )
    await mon.start()
    try:
        await client.simulate_incoming(msg1)
        await asyncio.wait_for(downloaded_evt.wait(), timeout=2.0)
        assert len(downloaded) == 1
        downloaded_evt.clear()

        # 直接调 download_one:模拟 sync 重新拉这条 media(同 file_id)
        med2 = _media_with_file_id("fid-Y")
        assert mon.downloader is not None
        out = await mon.downloader.download_one(msg_pk=0, media=med2)
        # 应命中 storage skip #1 → DONE, 不调 client.download_file
        assert out.download_status == MediaDownloadStatus.DONE
        assert out.object_key, "拷自 storage"
        # 无新下载事件
        assert len(downloaded) == 1
    finally:
        await mon.stop()


async def test_live_monitor_emits_message_edited_on_content_change(
    monitor, client, bus, storage
) -> None:
    """先推 (100,1,v1),再推同 id v2(模拟 updateMessageContent)→ 发 MessageEdited。

    MessageReceived 只发 1 次(v1 时),编辑后 MessageEdited 触发,storage 文本被覆盖。
    """
    from tgmonitor.core.events import MessageEdited, MessageReceived

    monitor.set_whitelist([100])
    received: list[MessageReceived] = []
    edited: list[MessageEdited] = []
    edited_evt = asyncio.Event()

    async def _on_edited(e: MessageEdited) -> None:
        edited.append(e)
        edited_evt.set()
    async def _on_recv(e: MessageReceived) -> None:
        received.append(e)
    bus.subscribe(MessageReceived, _on_recv)
    bus.subscribe(MessageEdited, _on_edited)

    await monitor.start()
    try:
        # 第 1 条:v1
        await client.simulate_incoming(
            make_message(channel_id=100, msg_id=1, text="v1")
        )
        await asyncio.sleep(0.2)
        assert len(received) == 1
        # 第 2 条:同 id,text v2 — _seen_ids 命中 → 走 _handle_edited
        await client.simulate_incoming(
            make_message(channel_id=100, msg_id=1, text="v2")
        )
        await asyncio.wait_for(edited_evt.wait(), timeout=2.0)
        # 仍仅 1 条 MessageReceived(编辑不发)
        assert len(received) == 1
        # 1 条 MessageEdited,text 已覆盖
        assert len(edited) == 1
        assert edited[0].message is not None
        assert edited[0].message.text == "v2"
        # storage 也已覆盖
        stored = await storage.get_message(100, 1)
        assert stored is not None and stored.text == "v2"
    finally:
        await monitor.stop()


async def test_edit_path_overwrites_text_views_forwards_edited_media(
    monitor, client, bus, storage
) -> None:
    """编辑覆盖所有可变字段:text / views / forwards / edited / media。

    不动 message.id 与原 author 等不变字段(dataclasses.replace)。
    """
    from datetime import UTC, datetime

    from tgmonitor.core.events import MessageEdited

    monitor.set_whitelist([100])
    edited: list[MessageEdited] = []
    edited_evt = asyncio.Event()

    async def _on_edited(e: MessageEdited) -> None:
        edited.append(e)
        edited_evt.set()
    bus.subscribe(MessageEdited, _on_edited)

    # 第 1 条:initial
    initial = make_message(channel_id=100, msg_id=1, text="v1")
    # 第 2 条:edit,改 text + views + forwards + edited + media
    edited_dto = MessageDTO(
        id=0, channel_id=100, telegram_msg_id=1,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        text="v2-edited",
        author="alice",
        views=200,
        forwards=15,
        edited=True,
        media=[_media_with_file_id("new-fid")],
    )

    await monitor.start()
    try:
        await client.simulate_incoming(initial)
        await asyncio.sleep(0.1)
        # 同 id,模拟 updateMessageContent — 改 fields
        await client.simulate_incoming(edited_dto)
        await asyncio.wait_for(edited_evt.wait(), timeout=2.0)

        stored = await storage.get_message(100, 1)
        assert stored is not None
        assert stored.text == "v2-edited"
        assert stored.views == 200
        assert stored.forwards == 15
        assert stored.edited is True
        assert len(stored.media) == 1
        assert stored.media[0].telegram_file_id == "new-fid"
        # author 未变(编辑字段表不含 author)
        assert stored.author == "alice"
    finally:
        await monitor.stop()


async def test_edit_path_when_storage_empty_saves_as_new(
    monitor, client, bus, storage
) -> None:
    """编辑路径 + storage 空(罕见)→ 当作新增保存,仍发 MessageEdited。

    模拟:手动往 _seen_ids 注入 key(假装已见过),然后推 (100,1)
    → _seen_ids 命中 → 走 _handle_edited → storage.get_message 返 None
    → save_message + MessageEdited(不发 MessageReceived)。
    """
    from tgmonitor.core.events import MessageEdited, MessageReceived

    monitor.set_whitelist([100])
    edited: list[MessageEdited] = []
    received: list[MessageReceived] = []

    async def _on_edited(e: MessageEdited) -> None:
        edited.append(e)
    async def _on_recv(e: MessageReceived) -> None:
        received.append(e)
    bus.subscribe(MessageEdited, _on_edited)
    bus.subscribe(MessageReceived, _on_recv)

    await monitor.start()
    try:
        # 手动在 _seen_ids 塞 (100, 1),但 storage 没这条消息
        monitor._seen_ids[(100, 1)] = None
        await client.simulate_incoming(
            make_message(channel_id=100, msg_id=1, text="edit-on-empty")
        )
        await asyncio.sleep(0.2)
        # MessageReceived 不发(编辑路径走 MessageEdited)
        assert received == []
        # MessageEdited 发,消息已落库
        assert len(edited) == 1
        assert edited[0].message.text == "edit-on-empty"
        stored = await storage.get_message(100, 1)
        assert stored is not None
        assert stored.text == "edit-on-empty"
    finally:
        await monitor.stop()


async def test_seen_ids_cache_evicts_when_over_limit(
    monitor, client, bus
) -> None:
    """推 10001 条不同 id → _seen_ids 收敛到 10000(OrderedDict LRU)。"""
    monitor.set_whitelist([100])
    await monitor.start()
    try:
        for mid in range(1, 10002):
            await client.simulate_incoming(
                make_message(channel_id=100, msg_id=mid, text=f"m-{mid}")
            )
        await asyncio.sleep(0.5)
        # LRU cap=10000
        assert len(monitor._seen_ids) == 10000
        # 最旧的(1-1=0)被踢
        assert (100, 1) not in monitor._seen_ids
        # 最新(10001)还在
        assert (100, 10001) in monitor._seen_ids
    finally:
        await monitor.stop()


async def test_delete_message_removes_orphan_bytes(
    bus, storage, objectstore, settings, client
) -> None:
    """2026-08-24 delete_message 清孤儿 bytes:唯一引用该 key 的 message 被删
    → refcount=0 → objects.delete 被调 → ObjectStore 里 key 不再存在。

    路径:storage 里一条 message 带 fid="fid-X" + object_key="media/abc.png"
    → delete_message(100, 1) → ObjectStore 上 "media/abc.png" 被真删。
    """
    import dataclasses

    from tgmonitor.core.dto import MediaDownloadStatus
    from tgmonitor.core.monitor.service import MonitorService

    # 落库 + 写入 ObjectStore
    await objectstore.put("media/abc.png", b"image-bytes", None)
    assert await objectstore.exists("media/abc.png")
    base = _media_with_file_id("fid-X")
    done = dataclasses.replace(
        base,
        object_key="media/abc.png",
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
        file_size=len(b"image-bytes"),
    )
    msg = make_message(channel_id=100, msg_id=1, text="", media=[done])
    await storage.save_message(msg)
    # 跑 MonitorService.delete_message
    mon = MonitorService(bus, client, storage, objectstore, settings)
    await mon.delete_message(100, 1)
    # message 已删
    assert await storage.get_message(100, 1) is None
    # refcount=0 → bytes 真删
    assert not await objectstore.exists("media/abc.png")


async def test_delete_message_keeps_bytes_when_referenced(
    bus, storage, objectstore, settings, client
) -> None:
    """同 file_id 两条 message 都用同一 key,删一条 → bytes 保留(另一条还在引用)。

    回归:refcount 没引入前,删消息会把同 key 的 bytes 一并误删,跨消息去重场景下
    另一条 message 的 media 变孤儿(reference 还在但找不到 bytes)。
    """
    import dataclasses

    from tgmonitor.core.dto import MediaDownloadStatus
    from tgmonitor.core.monitor.service import MonitorService

    await objectstore.put("media/shared.png", b"shared-bytes", None)
    # 同 file_id="fid-Y" + 同 key="media/shared.png" 的两条 message(模拟跨消息去重)
    base = _media_with_file_id("fid-Y")
    med = dataclasses.replace(
        base,
        object_key="media/shared.png",
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
        file_size=len(b"shared-bytes"),
    )
    await storage.save_message(make_message(
        channel_id=100, msg_id=10, text="first", media=[med],
    ))
    await storage.save_message(make_message(
        channel_id=100, msg_id=11, text="second", media=[med],
    ))
    mon = MonitorService(bus, client, storage, objectstore, settings)
    # 删其中一条
    await mon.delete_message(100, 10)
    # message 10 已删,message 11 还在
    assert await storage.get_message(100, 10) is None
    assert await storage.get_message(100, 11) is not None
    # key 仍被 message 11 引用 → bytes 保留
    assert await objectstore.exists("media/shared.png")
    assert await objectstore.get("media/shared.png") == b"shared-bytes"


def test_edited_message_ui_replace_message_renders_new_text():
    """UI 层:MessageView.replace_message 按 (channel_id, telegram_msg_id) 找 row,
    调 _format 重渲,row 数不变,文本更新到 v2。"""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841 — keep alive
    from datetime import UTC, datetime

    from tgmonitor.core.dto import MessageDTO
    from tgmonitor.ui.widgets.message_view import MessageView

    view = MessageView()
    # 频道标题缓存,让 _format 输出稳定
    view.set_channel_titles({100: "TNews"})
    # 先 append 一条 v1
    msg1 = MessageDTO(
        id=0, channel_id=100, telegram_msg_id=1,
        date=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        text="v1", author="alice", media=[],
    )
    view.append(msg1)
    assert view.count() == 1
    # 编辑事件触发 replace_message(v2)
    msg2 = MessageDTO(
        id=0, channel_id=100, telegram_msg_id=1,
        date=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        text="v2-edited", author="alice", media=[],
    )
    view.replace_message(msg2)
    # row 数不变
    assert view.count() == 1
    # 内容更新
    item = view.item(0)
    assert item is not None
    dto_after = item.data(MessageView._ROLE_DTO)
    assert isinstance(dto_after, MessageDTO)
    assert dto_after.text == "v2-edited"
    # 显示文本含 "v2-edited"
    assert "v2-edited" in item.text()


# ============================================================
# 2026-08-27 v1.4.0 PR #10:MessageInteractionsChanged → monitor 落库路由
# (reactions + views 增量)。不依赖真实 TDLib,直接 publish bus 事件。
# ============================================================


async def test_monitor_routes_interactions_changed_to_storage(
    monitor, storage, client, bus
) -> None:
    """PR #10:publish MessageInteractionsChanged → storage.update_message_interactions
    被调,字段一致写入。

    不调真实 update_message_interactions(那是 storage 单测职责);这里只
    断言 storage 收到了正确 (channel_id, msg_id, views, reactions)。
    """
    monitor.set_whitelist([100])
    # start() 才会订阅 bus — 否则 handler 不会触发
    await monitor.start()
    try:
        # 先存一条消息让 update 落到真实对象上
        base = MessageDTO(
            id=0, channel_id=100, telegram_msg_id=42,
            date=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            text="hi", author="alice", media=[],
        )
        await storage.save_message(base)

        new_rxns = [
            ReactionDTO(type="emoji", emoji="🎉", count=5, is_chosen=True),
        ]
        await bus.publish(MessageInteractionsChanged(
            channel_id=100, telegram_msg_id=42,
            views=999, reactions=new_rxns,
        ))
        # bus.publish 是 async,handler 也是 async → 让 event loop 跑一轮
        await asyncio.sleep(0)
        rows = await storage.list_messages(channel_ids=[100])
        got = next(r for r in rows if r.telegram_msg_id == 42)
        assert got.views == 999
        assert got.reactions is not None
        assert got.reactions[0].emoji == "🎉"
        assert got.reactions[0].is_chosen is True
    finally:
        await monitor.stop()


async def test_monitor_interactions_handler_swallows_errors(
    monitor, storage, client, bus, caplog
) -> None:
    """PR #10:storage 抛异常时 handler 不抛回 bus(其它订阅者不感知)。

    用 None storage 来强制异常。
    """
    await monitor.start()
    class BoomStorage:
        async def update_message_interactions(self, *a, **kw):
            raise RuntimeError("simulated")
    original = monitor.storage
    monitor.storage = BoomStorage()
    try:
        # handler 抛异常被 bus 吞,不冒泡到 publish 调用者
        await bus.publish(MessageInteractionsChanged(
            channel_id=100, telegram_msg_id=1, views=10, reactions=[],
        ))
        await asyncio.sleep(0)
    finally:
        monitor.storage = original
        await monitor.stop()
