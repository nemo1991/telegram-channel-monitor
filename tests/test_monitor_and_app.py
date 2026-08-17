"""Monitor + AppService + EventBus 端到端(无网络)。"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from tests.conftest import make_message
from tgmonitor.core.dto import MediaDTO, MessageDTO
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
