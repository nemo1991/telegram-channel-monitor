"""TDLib 客户端生命周期单元测试。

不实际启动 TDLib — 通过 monkey-patch `tdlib_json.TdlibJsonClient.__init__`
让 Client.__init__ 变成空操作,然后手动驱动我们的状态机。覆盖:
  - 状态机进展(每一跳)
  - `_set_state` 是唯写路径
  - `AuthErrorOccurred` 在验证码错时发出
  - start 超时检测到 401 → 返回 ("error", "...encryption key...")
  - 重复 send/emit 时 detail 字段不被吞
  - `_state_event` 在状态变化时 set,start() 等待后立即返回
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import pytest

# `stub_tdlib_init` 由 tests/conftest.py 统一提供(全局 fixture)
# — 不在 test_telegram_lifecycle.py 再 re-export,否则会 shadow conftest 版,
#   导致 tdlib_json stub 不生效,TdlibTelegramClient 构造触发 native 加载
#   libtdjson(本机未编译时直接失败)。
from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, Settings
from tgmonitor.core.events import AuthErrorOccurred, EventBus, LoginStateChanged
from tgmonitor.core.telegram import tdlib_client as tdc


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_id=1,
        api_hash="x" * 32,
        phone="+10000000000",
        session_dir=tmp_path / "session",
        db_root=tmp_path / "m",
        objectstore_root=tmp_path / "o",
        media_policy=MediaPolicy.METADATA,
        db_backend=DBBackend.JSONL,
        objectstore_backend=ObjectStoreBackend.LOCAL,
    )


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@contextlib.asynccontextmanager
async def make_client(settings, bus):  # type: ignore[no-untyped-def]
    """Build a TdlibTelegramClient with stubbed tdlib_json init."""
    client = tdc.TdlibTelegramClient(settings, event_bus=bus)
    # 强 stub 子类上一些父类方法以避免被真父类调用
    client._running = False  # 默认未跑
    try:
        yield client
    finally:
        # 不真 close — 父类可能炸
        try:
            client._streams.clear()
        except Exception:
            pass


# ============================================================
# _set_state 行为
# ============================================================


@pytest.mark.asyncio
async def test_set_state_emits_login_state_changed(settings, bus, stub_tdlib_init):
    captured: list[LoginStateChanged] = []

    async def _cap(e):
        if isinstance(e, LoginStateChanged):
            captured.append(e)

    bus.subscribe(LoginStateChanged, _cap)

    async with make_client(settings, bus) as client:
        client._set_state("phone_required", detail="first")
        # 等 publish task 跑完
        await asyncio.sleep(0.05)
        assert client._state == "phone_required"
        assert client._state_detail == "first"
        assert any(e.detail == "first" for e in captured)

        # 同状态 + 同 detail → 不再 publish
        before = len(captured)
        client._set_state("phone_required", detail="first")
        await asyncio.sleep(0.05)
        assert len(captured) == before

        # 状态变 + 新 detail → publish 新 detail(不被旧的 dedup 吞掉)
        client._set_state("phone_required", detail="second")
        await asyncio.sleep(0.05)
        assert client._state_detail == "second"
        assert any(e.detail == "second" for e in captured)


@pytest.mark.asyncio
async def test_set_state_signals_event(settings, bus, stub_tdlib_init):
    """_state_event 必须在状态变化时 set;wait_for 立刻返回。"""
    async with make_client(settings, bus) as client:
        # 初始状态 event 是 clear(因为没人 set 过)
        assert not client._state_event.is_set()
        # 给一个 awaiter 排队
        waiter = asyncio.create_task(asyncio.wait_for(client._state_event.wait(), timeout=1.0))
        # 让 waiter 有机会 subscribe
        await asyncio.sleep(0.01)
        client._set_state("phone_required")
        # waiter 应该立刻返回
        await waiter
        assert client._state_event.is_set()


# ============================================================
# submit_code / submit_password 错误路径
# ============================================================


@pytest.mark.asyncio
async def test_submit_code_wrong_publishes_auth_error(settings, bus, stub_tdlib_init):
    """验证码错 → 发 AuthErrorOccurred(source="code", ...),不切换顶层状态。"""

    class FakeUnauthorized(tdc.TdlibError):
        pass

    captured: list[AuthErrorOccurred] = []

    async def _cap(e):
        if isinstance(e, AuthErrorOccurred):
            captured.append(e)

    bus.subscribe(AuthErrorOccurred, _cap)

    async with make_client(settings, bus) as client:
        client._set_state("code_required")

        async def _bad_request(*args, **kwargs):
            raise FakeUnauthorized(code=401, message="PHONE_CODE_INVALID")

        client.request = _bad_request  # type: ignore[method-assign]

        await client._code_queue.put("00000")
        await asyncio.wait_for(client._check_authentication_code(), timeout=1.0)

        await asyncio.sleep(0.05)
        assert any(e.source == "code" for e in captured)
        assert client._state == "code_required"


@pytest.mark.asyncio
async def test_submit_password_wrong_publishes_auth_error(settings, bus, stub_tdlib_init):
    """2FA 密码错 → AuthErrorOccurred(source="password")。"""

    class FakeUnauthorized(tdc.TdlibError):
        pass

    captured: list[AuthErrorOccurred] = []

    async def _cap(e):
        if isinstance(e, AuthErrorOccurred):
            captured.append(e)

    bus.subscribe(AuthErrorOccurred, _cap)

    async with make_client(settings, bus) as client:
        client._set_state("password_required")

        async def _bad_request(*args, **kwargs):
            raise FakeUnauthorized(code=401, message="PASSWORD_HASH_INVALID")

        client.request = _bad_request  # type: ignore[method-assign]

        await client._password_queue.put("wrongpw")
        await asyncio.wait_for(client._check_authentication_password(), timeout=1.0)
        await asyncio.sleep(0.05)

        assert any(e.source == "password" for e in captured)
        assert client._state == "password_required"


# ============================================================
# start() 超时检测 401
# ============================================================


@pytest.mark.asyncio
async def test_start_timeout_with_401_returns_error_detail(settings, bus, stub_tdlib_init):
    """start 超时 + 我们看到 401 → 返回 ('error', '...encryption key...')。
    模拟:start 在 _do_start_inner 上挂住,我们通过 fake error 注入 401,
    然后手动让 _do_start_inner 抛 TimeoutError。
    """
    async with make_client(settings, bus) as client:
        client._state_event.clear()

        # 替换 _do_start_inner 让它先 inject 401(模拟 tdlib_json 把 TDLib Error
        # 推给我们)再抛 TimeoutError。start() 内部会清一次 deque,
        # 所以必须在 _do_start_inner 里 inject。
        async def _hang_with_401():
            client._seen_error_codes.append(401)
            raise TimeoutError()

        client._do_start_inner = _hang_with_401  # type: ignore[method-assign]
        client._run_preflight = _noop_preflight  # type: ignore[method-assign]

        state, detail = await client.start()
        assert state == "error"
        assert detail is not None
        assert "encryption key" in detail


@pytest.mark.asyncio
async def test_start_timeout_no_error_codes_returns_generic(settings, bus, stub_tdlib_init):
    """start 超时但没收到任何 error 码 → 报 'DC 不可达' 类。"""
    async with make_client(settings, bus) as client:
        client._state_event.clear()

        async def _hang():
            raise TimeoutError()

        client._do_start_inner = _hang  # type: ignore[method-assign]
        client._run_preflight = _noop_preflight  # type: ignore[method-assign]

        state, detail = await client.start()
        assert state == "error"
        assert detail is not None
        # 不含 "encryption key"
        assert "encryption key" not in detail


@pytest.mark.asyncio
async def test_settle_loop_waits_when_no_error_codes(settings, bus, stub_tdlib_init):
    """settle 宽限超时但没收到 error codes → 不杀,继续等状态推进。

    回归 2026-08-13 线上 bug:冷启动(SOCKS5 连 DC / restore 半成品 session)
    通常远超 `_SETTLE_GRACE`(5s),旧代码单次超时就 `_kill_client()` 转 error,
    30s `_BOOT_TIMEOUT` 预算形同虚设,用户看到「TDLib 启动超时」误报。
    """
    async with make_client(settings, bus) as client:
        client._SETTLE_GRACE = 0.05  # type: ignore[assignment]
        client._schedule_updates_loop = lambda: None  # type: ignore[method-assign]
        client.execute = _noop_async  # type: ignore[method-assign]
        client._setup_proxy = _noop_async  # type: ignore[method-assign]
        client._setup_options = _noop_async  # type: ignore[method-assign]
        client.send = _noop_async  # type: ignore[method-assign]

        client._set_state("tdlib_parameters")  # 停在瞬态,且不产生任何 error code
        task = asyncio.create_task(client._do_start_inner())
        # 熬过几个宽限周期 — 0 codes 时应继续等待,而不是立刻转 error
        await asyncio.sleep(0.25)
        assert not task.done(), "settle 循环不该在 0 codes 时提前杀 boot"
        assert client._state == "tdlib_parameters"

        client._set_state("ready")  # 状态推进 → 循环退出
        await asyncio.wait_for(task, timeout=2)
        assert client._state == "ready"


@pytest.mark.asyncio
async def test_settle_loop_fails_fast_when_error_codes_seen(settings, bus, stub_tdlib_init):
    """settle 宽限超时且已收到 error codes(被 TDLib 拒绝)→ 立即转可见错误。"""
    async with make_client(settings, bus) as client:
        client._SETTLE_GRACE = 0.05  # type: ignore[assignment]
        client._schedule_updates_loop = lambda: None  # type: ignore[method-assign]
        client.execute = _noop_async  # type: ignore[method-assign]
        client._setup_proxy = _noop_async  # type: ignore[method-assign]
        client._setup_options = _noop_async  # type: ignore[method-assign]
        client.send = _noop_async  # type: ignore[method-assign]

        client._set_state("tdlib_parameters")
        client._seen_error_codes.append(400)  # api_id/api_hash 无效被拒
        await asyncio.wait_for(client._do_start_inner(), timeout=2)
        assert client._state == "error"
        assert "DC 握手失败" in client._state_detail


async def _noop_async(*args, **kwargs):  # noqa: ANN002, ANN003
    return None


async def _noop_preflight():
    return True, None


# ============================================================
# AuthErrorOccurred 事件继承自 ErrorOccurred(向后兼容订阅)
# ============================================================


@pytest.mark.asyncio
async def test_auth_error_occured_subclasses_error_occurred(settings, bus, stub_tdlib_init):
    """AuthErrorOccurred 应被 ErrorOccurred 订阅者也接收(以前若有 widget 订阅父类)。"""
    from tgmonitor.core.events import ErrorOccurred

    parents: list[ErrorOccurred] = []

    async def _cap(e):
        if isinstance(e, ErrorOccurred):
            parents.append(e)

    bus.subscribe(ErrorOccurred, _cap)

    async with make_client(settings, bus) as client:
        await client._publish_auth_error("code", "wrong code")
        await asyncio.sleep(0.05)
        assert any(e.message == "wrong code" for e in parents)


# ============================================================
# close() drains code/password queues(防止下次 session 错读)
# ============================================================


@pytest.mark.asyncio
async def test_kill_drains_input_queues(settings, bus, stub_tdlib_init):
    async with make_client(settings, bus) as client:
        # _kill_client 走"只有 running 才干活"分支 — 强制打开
        client._running = True
        await client._code_queue.put("stale1")
        await client._password_queue.put("stale2")
        await client._kill_client()
        assert client._code_queue.empty()
        assert client._password_queue.empty()


# ============================================================
# _AUTH_STATE_MAP 覆盖所有 tdlib_json 关键状态
# ============================================================


def test_auth_state_map_covers_lifecycle_keys():
    keys = set(tdc._AUTH_STATE_MAP.keys())
    # 这些字符串是 TDLib 的 @type 串
    expected = {
        "authorizationStateWaitTdlibParameters",
        "authorizationStateWaitPhoneNumber",
        "authorizationStateWaitCode",
        "authorizationStateWaitPassword",
        "authorizationStateReady",
        "authorizationStateClosing",
        "authorizationStateClosed",
    }
    assert expected.issubset(keys)


@pytest.mark.asyncio
async def test_wait_code_action_not_awaited_inline(stub_tdlib_init):
    """回归:WaitCode 的 action 不能 inline await。

    否则 _updates_loop(唯一消费 socket 流的任务)会冻在用户输入等待上,
    checkAuthenticationCode 的响应永远读不到 → request 30s 超时。
    """
    from tdlib_json import TdlibJsonClient

    class BlockingCodeClient(TdlibJsonClient):
        def __init__(self) -> None:
            super().__init__(parameters={})
            self.code: asyncio.Queue[str] = asyncio.Queue()

        async def _ask_for_code(self) -> None:
            self.last_code = await self.code.get()

    c = BlockingCodeClient()
    c._running = True
    # 修复前:这里会卡在 queue.get() 上,wait_for 超时失败
    await asyncio.wait_for(
        c._on_authorization_state_update({"@type": "authorizationStateWaitCode"}),
        timeout=1.0,
    )
    await c.code.put("12345")
    for _ in range(50):
        if getattr(c, "last_code", None) == "12345":
            break
        await asyncio.sleep(0.02)
    assert getattr(c, "last_code", None) == "12345"


@pytest.mark.asyncio
async def test_auth_state_error_does_not_kill_updates_loop(stub_tdlib_init):
    """回归:auth-state handler 抛异常时 `_updates_loop` 必须继续派发后续事件。

    修复前 `except Exception: raise` 会把唯一消费 TDLib 推送的任务杀掉,
    之后实时消息和所有 `request()` 响应同时静默超时 — 表现为"一段时间不监听"。
    """
    from tdlib_json import TdlibJsonClient

    class _AuthErrorClient(TdlibJsonClient):
        def __init__(self) -> None:
            super().__init__(parameters={})
            self.handled: list = []

    class _FakeTd:
        async def receive(self):
            yield {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateWaitPhoneNumber"},
            }
            yield {"@type": "updateNewMessage", "message": {"id": 1}}

    c = _AuthErrorClient()
    c._running = True

    async def _boom(self, authorization_state) -> None:
        raise RuntimeError("boom")

    c._on_authorization_state_update = _boom  # type: ignore[method-assign]

    done = asyncio.Event()

    async def on_msg(client, update) -> None:
        c.handled.append(update.get("message", {}).get("id"))
        done.set()

    c.add_event_handler(on_msg, "updateNewMessage")
    c.tdjson_client = _FakeTd()
    task = asyncio.create_task(c._updates_loop())
    try:
        # 修复前:auth 异常把 loop 杀死 → done 永远不 set → wait_for 超时
        await asyncio.wait_for(done.wait(), timeout=2.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert c.handled == [1]


@pytest.mark.asyncio
async def test_updates_loop_crashes_and_restarts(stub_tdlib_init):
    """`_updates_loop` 意外崩溃后自动重启(带 1s 最小重启间隔)。"""
    from tdlib_json import TdlibJsonClient

    calls = {"n": 0}

    class _FlakyClient(TdlibJsonClient):
        def __init__(self) -> None:
            super().__init__(parameters={})

        async def _updates_loop(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")

    c = _FlakyClient()
    c._running = True
    c._schedule_updates_loop()
    try:
        # 崩溃回调 + 重启(首次 delay=0)只需几个事件循环轮次
        for _ in range(50):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.02)
        assert calls["n"] == 2
        assert c._update_task is not None and c._update_task.done()
        assert c._update_task.exception() is None
    finally:
        if c._update_task is not None and not c._update_task.done():
            c._update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await c._update_task


# ============================================================
# 关闭流程的 entry guard(回归:close race 不再撞 10s request 超时
#  + qasync 跨 loop wakeup RuntimeError)
# ============================================================


def _make_stubbed_client(settings: Settings, bus: EventBus) -> tdc.TdlibTelegramClient:
    """用空 stub 起一个真实 TdlibTelegramClient 实例。

    `stub_tdlib_init` 把 _AiClient.__init__ 变成空操作;我们直接
    `TdlibTelegramClient(settings, event_bus=bus)` 即可构造(其它 ctor
    参数走 `settings`)。
    """
    return tdc.TdlibTelegramClient(settings, event_bus=bus)


@pytest.mark.asyncio
async def test_resolve_channel_metadata_nested_attribute_access(
    settings, bus, stub_tdlib_init, monkeypatch
):
    """回归:getChat 的 type 嵌套 dict 必须支持属性访问。

    修复前 `TDLibObject.from_dict` 只包顶层、嵌套层是普通 dict,
    `_resolve_channel_metadata` 里 `ct.supergroup_id` 直接 AttributeError
    (日志: `_resolve_channel_metadata(-1001125107539) failed`)。
    """
    from tdlib_json import TDLibObject

    client = _make_stubbed_client(settings, bus)
    responses = {
        "getChat": {
            "@type": "chat",
            "id": -1001,
            "title": "测试频道",
            "type": {
                "@type": "chatTypeSupergroup",
                "is_channel": True,
                "supergroup_id": 999,
            },
        },
        "getSupergroup": {
            "@type": "supergroup",
            "usernames": {"active_usernames": ["chn_username"]},
            "member_count": 42,
        },
    }

    async def fake_request(query: dict, **kwargs: object) -> TDLibObject:
        return TDLibObject.from_dict(responses[query["@type"]])

    monkeypatch.setattr(client, "request", fake_request)
    dto = await client.channels._resolve_channel_metadata(-1001)
    assert dto is not None
    assert dto.kind == "channel"
    assert dto.title == "测试频道"
    assert dto.username == "chn_username"
    assert dto.member_count == 42


def test_list_joined_channels_returns_empty_when_closing(settings, bus, stub_tdlib_init, caplog):
    """VM refresh 在 client 关闭时 fire-and-forget 调 list_joined_channels
    → 应静默返回 [],不抛,不刷 traceback。这是 2026-07-17 启动 race 的
    修复主断言。
    """
    import logging

    client = _make_stubbed_client(settings, bus)
    # 模拟 close() 已经设标志
    client._closing = True
    with caplog.at_level(logging.INFO):
        result = asyncio.run(client.list_joined_channels())
    assert result == []
    # 不应该出现 traceback 异常记录(只应有 INFO「client closing」一句)
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], (
        f"list_joined_channels 在 closing 时不应记 ERROR;got "
        f"{[(r.levelname, r.name, r.getMessage()) for r in error_records]}"
    )


def test_submit_phone_raises_client_closing_when_closing(settings, bus, stub_tdlib_init):
    """事务性方法(submit_phone / submit_code / 等)在 closing 时抛
    ClientClosingError,让调用方按自己策略处理 — 但不撞 tdlib_json bridge、
    不再等 10s request_timeout。
    """
    client = _make_stubbed_client(settings, bus)
    client._closing = True

    async def _go() -> None:
        with pytest.raises(tdc.ClientClosingError):
            await client.submit_phone("+8612345")
        with pytest.raises(tdc.ClientClosingError):
            await client.submit_code("12345")
        with pytest.raises(tdc.ClientClosingError):
            await client.submit_password("hunter2")
        with pytest.raises(tdc.ClientClosingError):
            await client.logout()
        with pytest.raises(tdc.ClientClosingError):
            await client.start()

    asyncio.run(_go())


def test_close_sets_closing_flag(settings, bus, stub_tdlib_init):
    """close() 是入口 contract:第一件事就是 _closing=True,
    这样任何后续 entry 都立刻 throw。
    """
    client = _make_stubbed_client(settings, bus)
    assert client._closing is False

    async def _go() -> None:
        await client.close()

    asyncio.run(_go())
    assert client._closing is True


@pytest.mark.parametrize(
    "state", ["uninit", "phone_required", "code_required", "password_required", "error"]
)
def test_list_joined_channels_returns_empty_when_state_not_ready(
    settings, bus, stub_tdlib_init, state, caplog
):
    """VM 的 bootstrap_ui 在 app 启动后立刻 fire-and-forget 调 list_joined_channels,
    这时 bridge 还在 `_state in {uninit, phone_required, code_required, ...}` 中。
    现在策略:
      - 非 ready 时**等**最多 N 秒让 state 走到 ready(best-effort 救用户)
      - N 秒超时 / state 永远不到 ready,返回 `[]` + DEBUG log
      - 不撞 tdlib_json bridge,不再 10s request_timeout

    2026-07-18 早实测:`RuntimeError: loop ... is not the running loop` 后立刻跟
    `list_joined_channels failed` 10s 超时 —— bridge 没 ready,VM 硬拉,撞 tdlib_json
    内部排队的 cross-loop wakeup。
    """
    import logging

    client = _make_stubbed_client(settings, bus)
    client._state = state  # 不走 start(),直接拨成中间态
    # 把 wait timeout 设小,让测试快 — 验证 "非 ready 不动 + 超时返 []"
    # (event 未 set 时 `_wait_for_state` 单次粒度 0.5s,5 例 ≈2.5s)
    client.channels._READY_WAIT_TIMEOUT = 0.05
    assert client._closing is False  # 确保 readiness 检查才是关键,_closing=False

    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(client.list_joined_channels())
    assert result == []
    # 应该 print 出"未到 ready"的 DEBUG 一行
    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("state=" in r.getMessage() and "未到 ready" in r.getMessage() for r in debug_msgs), (
        f"expected DEBUG 'state=… 未到 ready' 记录;got {[r.getMessage() for r in caplog.records]}"
    )
    # 不应该 ERROR 级别
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == []


def test_list_joined_channels_in_ready_state_still_calls_request(settings, bus, stub_tdlib_init):
    """最简回归:`_state="ready"` 时不应该被新 guard 拦截,应该真的进
    `request(GetChats)`。stub tdlib_json 让 `request` 抛一个洞,我们只断它被调
    到 / 怎么到的。
    """
    client = _make_stubbed_client(settings, bus)
    client._state = "ready"

    called = []

    async def _fake_request(req):  # type: ignore[no-untyped-def]
        called.append(req)

        # 模拟 TDLib 返回空 chat 列表(常见 — 没新消息 / 拒访)
        class _R:
            chat_ids: list = []

        return _R()

    client.request = _fake_request  # type: ignore[method-assign]

    result = asyncio.run(client.list_joined_channels())
    assert result == []
    assert len(called) == 1  # 进了一步,没被早返拦下


# ============================================================
# list_joined_channels 拆出的 _iter_resolved_chats 异步生成器
# ============================================================


async def _collect_iter(client, chat_ids):
    """async helper — 跑完 _iter_resolved_chats,返回 yield 出来的 DTO 列表。

    注(2026-08-02 composition 拆分后):`_iter_resolved_chats` 现在挂在
    `client.channels` 上,不在 `client` 上。
    """
    from tgmonitor.core.telegram.tdlib_client import ChannelDTO  # noqa: F401

    out: list = []
    async for dto in client.channels._iter_resolved_chats(chat_ids, t0=0.0):
        out.append(dto)
    return out


def test_iter_resolved_chats_yields_all_dtos(
    settings: Settings,
    bus: EventBus,
    stub_tdlib_init,
) -> None:
    """正常路径:每个 cid 解析成功 → 全 yield,顺序保持。"""
    from tgmonitor.core.dto import ChannelDTO

    client = _make_stubbed_client(settings, bus)
    # stub 解析函数 — 第 1/3/5 个返回 channel,第 2/4 返回 None(skip)
    calls: list[int] = []

    async def _fake_resolve(cid: int) -> ChannelDTO | None:
        calls.append(cid)
        if cid % 2 == 0:
            return None  # private/secret
        return ChannelDTO(id=cid, title=f"#{cid}", kind="channel")

    client.channels._resolve_channel_metadata = _fake_resolve  # type: ignore[method-assign]

    got = asyncio.run(_collect_iter(client, [1, 2, 3, 4, 5]))
    # 只 yield 非 None,顺序保持
    assert [d.id for d in got] == [1, 3, 5]
    # 每个 cid 至少 try 一次
    assert sorted(calls) == [1, 2, 3, 4, 5]


def test_iter_resolved_chats_skips_failed_resolve(
    settings: Settings,
    bus: EventBus,
    stub_tdlib_init,
) -> None:
    """单条 `_resolve_channel_metadata` 抛 Exception → log + skip,其他继续。"""
    from tgmonitor.core.dto import ChannelDTO

    client = _make_stubbed_client(settings, bus)

    async def _fake_resolve(cid: int) -> ChannelDTO | None:
        if cid == 2:
            raise RuntimeError("resolve exploded")
        return ChannelDTO(id=cid, title=f"#{cid}", kind="channel")

    client.channels._resolve_channel_metadata = _fake_resolve  # type: ignore[method-assign]

    got = asyncio.run(_collect_iter(client, [1, 2, 3]))
    # cid=2 抛错被 skip,1 和 3 正常 yield
    assert [d.id for d in got] == [1, 3]


def test_iter_resolved_chats_propagates_client_closing(
    settings: Settings,
    bus: EventBus,
    stub_tdlib_init,
) -> None:
    """mid-loop `_check_alive()` 抛 ClientClosingError → 立刻被 caller 捕获,
    不会把单条继续排进 TDLib bridge(原 `_check_alive` 设的边界保持)。"""
    from tgmonitor.core.dto import ChannelDTO

    client = _make_stubbed_client(settings, bus)
    client._closing = True  # 让 _check_alive 抛 ClientClosingError

    async def _fake_resolve(cid: int) -> ChannelDTO | None:
        return ChannelDTO(id=cid, title=f"#{cid}", kind="channel")

    client.channels._resolve_channel_metadata = _fake_resolve  # type: ignore[method-assign]

    # mid-loop close() 抛 ClientClosingError,不再 _resolve_channel_metadata
    with pytest.raises(tdc.ClientClosingError):
        asyncio.run(_collect_iter(client, [1, 2, 3]))


def test_iter_resolved_chats_empty_input_yields_nothing(
    settings: Settings,
    bus: EventBus,
    stub_tdlib_init,
) -> None:
    """`GetChats` 返空列表(`chat_ids=None` 走完 `or []` 是空)→ 立刻结束,
    不进 `_check_alive()`,不调 `_resolve_channel_metadata`。"""
    from tgmonitor.core.dto import ChannelDTO

    client = _make_stubbed_client(settings, bus)
    calls: list[int] = []

    async def _fake_resolve(cid: int) -> ChannelDTO | None:
        calls.append(cid)
        return ChannelDTO(id=cid, title=f"#{cid}", kind="channel")

    client.channels._resolve_channel_metadata = _fake_resolve  # type: ignore[method-assign]

    got = asyncio.run(_collect_iter(client, []))
    assert got == []
    assert calls == []  # 不应调任何解析


# ---- UpdateStream 生命周期:aclose 自动从 client._streams 移除 ----


def test_subscribe_updates_adds_stream_and_aclose_removes_it(
    settings: Settings, bus: EventBus, stub_tdlib_init
) -> None:
    """长会话回归:每次 `subscribe_updates()` 把 stream 加进 client._streams,
    caller 调 `aclose()` 必须从列表移除 — 避免 leak。

    之前(commit 18ddd19 之前):只在 client.close() 一次性清空,subscribe 即使
    立刻 aclose 仍占用,长会话该列表只增不减。
    """
    client = _make_stubbed_client(settings, bus)

    s1 = client.subscribe_updates()
    s2 = client.subscribe_updates()
    assert len(client._streams) == 2

    # aclose 一个,列表减 1
    asyncio.run(s1.aclose())
    assert len(client._streams) == 1
    assert s2 in client._streams

    # aclose 剩下的,列表空
    asyncio.run(s2.aclose())
    assert client._streams == []

    # aclose 重复 idempotent:不抛
    asyncio.run(s1.aclose())
    assert client._streams == []


def test_subscribe_updates_streams_receive_push_until_aclose(
    settings: Settings, bus: EventBus, stub_tdlib_init
) -> None:
    """功能回归:aclose 之前 push 仍能收到;aclose 之后 push 是 no-op(不抛)。"""
    from tgmonitor.core.dto import MessageDTO

    client = _make_stubbed_client(settings, bus)
    stream = client.subscribe_updates()

    async def _scenario() -> None:
        msg = MessageDTO(
            id=1,
            channel_id=1,
            telegram_msg_id=1,
            text="hi",
            date=datetime.now(UTC),
            media=[],
        )
        # 订阅后 push → 收到
        await stream.push(msg)
        got = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
        assert got.text == "hi"

        # aclose 后:sentinel 已塞,__anext__ 立即抛 StopAsyncIteration
        await stream.aclose()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(stream.__anext__(), timeout=1.0)

        # 幂等:重复 aclose 不抛
        await stream.aclose()

        # 已 aclose 的 stream 二次 push 静默 no-op(stream 本身仍可调 push,
        # 内部 _closed 守门)
        msg2 = MessageDTO(
            id=2,
            channel_id=1,
            telegram_msg_id=2,
            text="after",
            date=datetime.now(UTC),
            media=[],
        )
        await stream.push(msg2)  # 不抛

    asyncio.run(_scenario())


# ============================================================
# 凭据预检 — 未配置时抛 TelegramNotConfiguredError(而非 pydantic
# ValidationError,运行报错 api_id=0 场景的回归)
# ============================================================


@pytest.mark.parametrize(
    "api_id, api_hash, phone, expected_fragment",
    [
        (0, "x" * 32, "+10000000000", "TG_API_ID"),
        (1, "", "+10000000000", "TG_API_HASH"),
        (1, "x" * 32, "", "TG_PHONE"),
        (1, "x" * 32, "10000000000", "TG_PHONE"),  # 缺 + 国家区号
    ],
)
def test_init_raises_not_configured_when_credentials_missing(
    tmp_path,
    bus,
    stub_tdlib_init,
    api_id,
    api_hash,
    phone,
    expected_fragment,
) -> None:
    """凭据未配置时构造必须抛 TelegramNotConfiguredError,消息带缺失项名。

    这是「api_id=0 启动崩 ValidationError」的守卫回归:不碰
    `TdlibJsonClient` 的 parameters 校验,直接给用户可读中文提示。
    """
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_id=api_id,
        api_hash=api_hash,
        phone=phone,
        session_dir=tmp_path / "session",
    )
    with pytest.raises(tdc.TelegramNotConfiguredError) as ei:
        tdc.TdlibTelegramClient(s, event_bus=bus)
    assert expected_fragment in str(ei.value)


def test_missing_credentials_lists_all_missing_items(
    tmp_path,
    stub_tdlib_init,
) -> None:
    """三项全缺 → 缺失项列表同时包含 api_id / api_hash / phone。"""
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_id=0,
        api_hash="",
        phone="",
        session_dir=tmp_path / "session",
    )
    missing = tdc._missing_credentials(s)
    assert "TG_API_ID" in missing
    assert "TG_API_HASH" in missing
    assert any(x.startswith("TG_PHONE") for x in missing)


# ============================================================
# 工厂占位 client — 凭据未配置时返回 UnconfiguredTelegramClient,
# 应用可正常启动进 UI(显示"未登录"引导),而非启动即崩溃
# ============================================================


def test_factory_returns_placeholder_when_credentials_missing(tmp_path) -> None:
    """凭据缺失 → factory 返回占位 client(state=phone_required,不抛)。

    这是「无 .env 启动即弹窗退出」bug 的回归:应用必须能启动,让用户在
    设置 → 账户 填好凭据。占位 client 不构造真 TdlibTelegramClient,也
    不需要 TDLib stub。
    """
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_id=0,
        api_hash="",
        phone="",
        session_dir=tmp_path / "session",
        db_root=tmp_path / "m",
        objectstore_root=tmp_path / "o",
        media_policy=MediaPolicy.METADATA,
        db_backend=DBBackend.JSONL,
        objectstore_backend=ObjectStoreBackend.LOCAL,
    )
    from tgmonitor.core.telegram.factory import build_telegram_client
    from tgmonitor.core.telegram.unconfigured import UnconfiguredTelegramClient

    client = build_telegram_client(s, use_fake=False)
    assert isinstance(client, UnconfiguredTelegramClient)
    assert client.state == "phone_required"
    assert client.me is None


async def test_placeholder_start_and_channels_safe() -> None:
    """占位 client 的 start / 频道接口给出安全默认值,不抛异常。"""
    from tgmonitor.core.telegram.unconfigured import UnconfiguredTelegramClient

    client = UnconfiguredTelegramClient()
    state, detail = await client.start()
    assert state == "phone_required"
    assert detail  # 引导文案非空
    assert await client.list_joined_channels() == []
    assert await client.download_file("whatever") is None
    # 防御接口:无凭据不可 join / 拉元数据 → 抛 TelegramNotConfiguredError
    with pytest.raises(tdc.TelegramNotConfiguredError):
        await client.join_channel("@example")
    with pytest.raises(tdc.TelegramNotConfiguredError):
        await client.get_channel_metadata(1)
    # 历史为空迭代
    assert [m async for m in client.iter_chat_history(1)] == []


async def test_placeholder_subscribe_stream_aclose_wakes_anext() -> None:
    """占位更新流永不结束;`aclose()` 唤醒 `__anext__` 退出 — monitor
    stop() 的关流流程与之自洽。"""
    from tgmonitor.core.telegram.unconfigured import UnconfiguredTelegramClient

    client = UnconfiguredTelegramClient()
    stream = client.subscribe_updates()
    # 未 close 时 __anext__ 阻塞(永不结束)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(stream.__anext__(), timeout=0.05)
    await stream.aclose()
    # close 后 __anext__ 抛 StopAsyncIteration(不再挂住 loop)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(stream.__anext__(), timeout=0.05)
    # aclose 幂等
    await stream.aclose()


def test_factory_builds_real_client_when_credentials_present(
    settings,
    bus,
    stub_tdlib_init,
) -> None:
    """凭据齐全 → factory 仍构造真 TdlibTelegramClient(占位仅缺凭据时)。"""
    from tgmonitor.core.telegram.factory import build_telegram_client

    client = build_telegram_client(settings, use_fake=False, event_bus=bus)
    assert isinstance(client, tdc.TdlibTelegramClient)


# ============================================================
# updateConnectionState → ConnectionStateChanged(底部状态栏)
# ============================================================


def test_conn_state_map_covers_lifecycle_keys() -> None:
    """`_CONN_STATE_MAP` 覆盖 TDLib 全部连接状态 @type(漏一个状态栏就停摆)。"""
    keys = set(tdc._CONN_STATE_MAP.keys())
    expected = {
        "connectionStateWaitingForNetwork",
        "connectionStateConnecting",
        "connectionStateUpdating",
        "connectionStateReady",
    }
    assert expected.issubset(keys)


@pytest.mark.asyncio
async def test_connection_state_publishes_event(settings, bus, stub_tdlib_init) -> None:
    """updateConnectionState(嵌套 connectionStateReady)→ bus 发 ConnectionStateChanged。

    注意:真实 update 永远先经 `TDLibObject.from_dict()` 包装再进 handler,
    测试必须传 TDLibObject(裸 dict 没有 `.state` 属性 → 状态解析为 unknown)。
    """
    from tdlib_json import TDLibObject

    from tgmonitor.core.events import ConnectionStateChanged

    captured: list[ConnectionStateChanged] = []

    async def _cap(e: object) -> None:
        if isinstance(e, ConnectionStateChanged):
            captured.append(e)

    bus.subscribe(ConnectionStateChanged, _cap)

    async with make_client(settings, bus) as client:
        update = TDLibObject.from_dict(
            {"@type": "updateConnectionState", "state": {"@type": "connectionStateReady"}}
        )
        await client._on_connection_state(client, update)
        await asyncio.sleep(0.05)
        assert any(e.state == "ready" for e in captured)


@pytest.mark.asyncio
async def test_do_start_inner_proxy_error_sets_error_state(
    settings,
    bus,
    stub_tdlib_init,
) -> None:
    """`_setup_proxy` 失败(代理设置错误)→ 启动直接转 error,不再走状态机。

    回归 2026-08-13:代理配了但 addProxy 被拒,旧代码 fire-and-forget 静默失败,
    用户看到"未连接";现在 UI 应看到「代理设置失败: …」。
    """
    async with make_client(settings, bus) as client:
        client._schedule_updates_loop = lambda: None  # type: ignore[method-assign]
        client.execute = _noop_async  # type: ignore[method-assign]
        client._setup_options = _noop_async  # type: ignore[method-assign]
        client.send = _noop_async  # type: ignore[method-assign]

        async def _bad_proxy() -> None:
            raise tdc.TdlibError(code=400, message="addProxy failed")

        client._setup_proxy = _bad_proxy  # type: ignore[method-assign]
        await asyncio.wait_for(client._do_start_inner(), timeout=2)
        assert client._state == "error"
        assert "代理设置失败" in client._state_detail
