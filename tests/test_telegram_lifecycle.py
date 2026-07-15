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
from typing import Iterator

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
def stub_aiotdlib_init() -> Iterator[None]:
    """把 aiotdlib.Client.__init__ 换成 no-op,跳过文件检查 + 默认参数验证。"""
    original = tdc._AiClient.__init__

    def _safe_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # 不调 super,只塞一些 aiotdlib 期望的内部状态,这样 _updates_loop
        # 之类的占位方法不至于 AttributeError
        self._update_task = None
        self._running = False
        self._handlers_tasks = set()
        self._pending_requests = {}
        self._pending_messages = {}
        self._updates_handlers = {}
        self._authorized_event = asyncio.Event()
        self._state = ""
        self._middlewares = []
        self._middlewares_handlers = []
        self.tdjson_client = type(
            "StubTd", (), {"receive": _async_iter([]), "send": _noop_send, "close": _noop_close, "execute": _noop_execute},
        )()
        # 父类期望的
        self.settings = kwargs.get("settings") or (
            args[0] if args else None
        )

    def _noop_send(*a, **k):
        return None

    async def _noop_close(*a, **k):
        return None

    async def _noop_execute(*a, **k):
        return None

    async def _async_iter(items):
        for x in items:
            yield x

    tdc._AiClient.__init__ = _safe_init  # type: ignore[assignment]
    try:
        yield
    finally:
        tdc._AiClient.__init__ = original  # type: ignore[assignment]


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