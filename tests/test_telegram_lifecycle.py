"""TDLib 客户端生命周期单元测试。

不实际启动 TDLib — 通过 monkey-patch `aiotdlib.Client.__init__` 让 Client.__init__
变成空操作,然后手动驱动我们的状态机。覆盖:
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

import pytest

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


@pytest.fixture
def stub_aiotdlib_init():
    """stub 已在 tests/conftest.py 统一提供,这里 re-export 仅为向后兼容
    旧 import 语句。"""
    yield


@contextlib.asynccontextmanager
async def make_client(settings, bus):  # type: ignore[no-untyped-def]
    """Build a TdlibTelegramClient with stubbed aiotdlib init."""
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
async def test_set_state_emits_login_state_changed(settings, bus, stub_aiotdlib_init):
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
async def test_set_state_signals_event(settings, bus, stub_aiotdlib_init):
    """_state_event 必须在状态变化时 set;wait_for 立刻返回。"""
    async with make_client(settings, bus) as client:
        # 初始状态 event 是 clear(因为没人 set 过)
        assert not client._state_event.is_set()
        # 给一个 awaiter 排队
        waiter = asyncio.create_task(
            asyncio.wait_for(client._state_event.wait(), timeout=1.0)
        )
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
async def test_submit_code_wrong_publishes_auth_error(settings, bus, stub_aiotdlib_init):
    """验证码错 → 发 AuthErrorOccurred(source="code", ...),不切换顶层状态。"""

    class FakeUnauthorized(tdc.AioTDLibError):
        def __init__(self, code: int, message: str) -> None:
            self.code = code
            self.message = message
            super().__init__(message)

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
async def test_submit_password_wrong_publishes_auth_error(settings, bus, stub_aiotdlib_init):
    """2FA 密码错 → AuthErrorOccurred(source="password")。"""

    class FakeUnauthorized(tdc.AioTDLibError):
        def __init__(self, code: int, message: str) -> None:
            self.code = code
            self.message = message
            super().__init__(message)

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
async def test_start_timeout_with_401_returns_error_detail(settings, bus, stub_aiotdlib_init):
    """start 超时 + 我们看到 401 → 返回 ('error', '...encryption key...')。
    模拟:start 在 _do_start_inner 上挂住,我们通过 fake error 注入 401,
    然后手动让 _do_start_inner 抛 TimeoutError。
    """
    async with make_client(settings, bus) as client:
        client._state_event.clear()

        # 替换 _do_start_inner 让它先 inject 401(模拟 aiotdlib 把 TDLib Error
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
async def test_start_timeout_no_error_codes_returns_generic(settings, bus, stub_aiotdlib_init):
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


async def _noop_preflight():
    return True, None


# ============================================================
# AuthErrorOccurred 事件继承自 ErrorOccurred(向后兼容订阅)
# ============================================================

@pytest.mark.asyncio
async def test_auth_error_occured_subclasses_error_occurred(settings, bus, stub_aiotdlib_init):
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
async def test_kill_drains_input_queues(settings, bus, stub_aiotdlib_init):
    async with make_client(settings, bus) as client:
        # _kill_aiotdlib 走"只有 running 才干活"分支 — 强制打开
        client._running = True
        await client._code_queue.put("stale1")
        await client._password_queue.put("stale2")
        await client._kill_aiotdlib()
        assert client._code_queue.empty()
        assert client._password_queue.empty()


# ============================================================
# _AUTH_STATE_MAP 覆盖所有 aiotdlib 关键状态
# ============================================================

def test_auth_state_map_covers_lifecycle_keys():
    keys = set(tdc._AUTH_STATE_MAP.keys())
    # 这些字符串是 aiotdlib 内部的 @type 串
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


# ============================================================
# 关闭流程的 entry guard(回归:close race 不再撞 10s request 超时
#  + qasync 跨 loop wakeup RuntimeError)
# ============================================================


def _make_stubbed_client(settings: Settings, bus: EventBus) -> tdc.TdlibTelegramClient:
    """用空 stub 起一个真实 TdlibTelegramClient 实例。

    `stub_aiotdlib_init` 把 _AiClient.__init__ 变成空操作;我们直接
    `TdlibTelegramClient(settings, event_bus=bus)` 即可构造(其它 ctor
    参数走 `settings`)。
    """
    return tdc.TdlibTelegramClient(settings, event_bus=bus)


def test_list_joined_channels_returns_empty_when_closing(
    settings, bus, stub_aiotdlib_init, caplog
):
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


def test_submit_phone_raises_client_closing_when_closing(
    settings, bus, stub_aiotdlib_init
):
    """事务性方法(submit_phone / submit_code / 等)在 closing 时抛
    ClientClosingError,让调用方按自己策略处理 — 但不撞 aiotdlib bridge、
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


def test_close_sets_closing_flag(settings, bus, stub_aiotdlib_init):
    """close() 是入口 contract:第一件事就是 _closing=True,
    这样任何后续 entry 都立刻 throw。
    """
    client = _make_stubbed_client(settings, bus)
    assert client._closing is False

    async def _go() -> None:
        await client.close()

    asyncio.run(_go())
    assert client._closing is True


@pytest.mark.parametrize("state", ["uninit", "phone_required", "code_required", "password_required", "error"])
def test_list_joined_channels_returns_empty_when_state_not_ready(
    settings, bus, stub_aiotdlib_init, state, caplog
):
    """VM 的 bootstrap_ui 在 app 启动后立刻 fire-and-forget 调 list_joined_channels,
    这时 bridge 还在 `_state in {uninit, phone_required, code_required, ...}` 中。
    现在策略:
      - 非 ready 时**等**最多 N 秒让 state 走到 ready(best-effort 救用户)
      - N 秒超时 / state 永远不到 ready,返回 `[]` + DEBUG log
      - 不撞 aiotdlib bridge,不再 10s request_timeout

    2026-07-18 早实测:`RuntimeError: loop ... is not the running loop` 后立刻跟
    `list_joined_channels failed` 10s 超时 —— bridge 没 ready,VM 硬拉,撞 aiotdlib
    内部排队的 cross-loop wakeup。
    """
    import logging
    client = _make_stubbed_client(settings, bus)
    client._state = state  # 不走 start(),直接拨成中间态
    assert client._closing is False  # 确保 readiness 检查才是关键,_closing=False

    with caplog.at_level(logging.DEBUG):
        # 把 wait timeout 设小,让测试快 — 验证 "非 ready 不动 + 超时返 []"
        result = asyncio.run(client.list_joined_channels())
    assert result == []
    # 应该 print 出"未到 ready"的 DEBUG 一行
    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(
        "state=" in r.getMessage() and "未到 ready" in r.getMessage()
        for r in debug_msgs
    ), (
        f"expected DEBUG 'state=… 未到 ready' 记录;got "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    # 不应该 ERROR 级别
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == []


def test_list_joined_channels_in_ready_state_still_calls_request(
    settings, bus, stub_aiotdlib_init
):
    """最简回归:`_state="ready"` 时不应该被新 guard 拦截,应该真的进
    `request(GetChats)`。stub aiotdlib 让 `request` 抛一个洞,我们只断它被调
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