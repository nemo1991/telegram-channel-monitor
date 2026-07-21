"""MainWindow 在 logged-in state 下应能展示已订阅 + 已加入频道的端到端回归测试。

背景(2026-07-18 用户反馈):
  "已监听和已加入的频道在登录状态下打开应用的时候未显示"

模拟:app 启动时已 valid session,_state="ready",storage 里已有订阅记录,
fake client 持有几个频道(channel 面板要把它们拉回来 + 与白名单求交集)。

不在测试里跑 qasync run_forever — 自己在后台线程起一个 asyncio loop
(模拟 qasync 的 QEventLoop),drive 它来跑协程。
"""
from __future__ import annotations

import asyncio
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import EventBus
from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.core.telegram.fake_client import FakeTelegramClient
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel

# `stub_aiotdlib_init` fixture 由 tests/conftest.py 统一提供


class _LoopThread:
    """后台线程跑一个持续运行的 asyncio loop — 模拟 qasync 的 QEventLoop。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def qloop() -> _LoopThread:
    """后台线程 + run_forever loop — 模拟 qasync 主线程 loop。"""
    lt = _LoopThread()
    yield lt.loop
    # stop + close loop
    lt.loop.call_soon_threadsafe(lt.loop.stop)
    lt._thread.join(timeout=2.0)
    try:
        lt.loop.close()
    except Exception:  # noqa: BLE001
        pass


def _wait_for_sync(loop, pred, *, timeout: float = 2.0, step: float = 0.02) -> bool:
    """在后台 loop 上同步等待 pred() 满足 — 用 background loop 做 polling。
    测试主体线程就是 main thread,所以 step 用 time.sleep 比较简单。
    """
    import time as _t
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        async def _check() -> bool:
            return bool(pred())

        # schedule pred() on background loop; block until it returns
        fut = asyncio.run_coroutine_threadsafe(_check(), loop)
        try:
            ok = fut.result(timeout=step)
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            return True
    return False


def test_vm_bootstrap_populates_known_channels_in_logged_in_state(qapp, qloop):
    """logged-in(state="ready")开 app 后,VM.bootstrap_ui 应拉回已加入频道
    填 known_channels,再 emit channels_changed 让 UI 渲染。

    失败模式(回归保护):
      - guard `if self._state != "ready"` 不应误判 (client 处于 ready)
      - run_coroutine_threadsafe 调度后,fire-and-forget 协程确实被 loop tick
      - channels 列表非空
    """
    import tempfile
    from pathlib import Path

    from tests.conftest import InMemoryRepository
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, Settings
    from tgmonitor.core.objectstore.local_store import LocalObjectStore

    with tempfile.TemporaryDirectory() as td:
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            api_id=1, api_hash="x" * 32, phone="+8612345",
            session_dir=Path(td) / "s",
            db_root=Path(td) / "m",
            objectstore_root=Path(td) / "o",
            media_policy=MediaPolicy.METADATA,
            db_backend=DBBackend.JSONL,
            objectstore_backend=ObjectStoreBackend.LOCAL,
        )
        settings.ensure_dirs()

        bus = EventBus()
        client = FakeTelegramClient()
        # 推到 "ready" 状态:本测试里直接赋属性(避免跑完整 login flow)
        client._state = "ready"
        # 注入 3 个已加入的频道
        for cid, title in [(100, "新闻"), (200, "技术"), (300, "财经")]:
            client.add_channel(ChannelDTO(id=cid, title=title))

        async def setup_async() -> tuple:
            storage = InMemoryRepository()
            await storage.connect()
            # 已订阅 100、200
            await storage.upsert_channel(ChannelDTO(id=100, title="新闻", is_subscribed=True))
            await storage.upsert_channel(ChannelDTO(id=200, title="技术", is_subscribed=True))

            objects = LocalObjectStore(root=Path(td) / "o")
            await objects.connect()

            monitor = MonitorService(bus, client, storage, objects, settings)
            app_svc = AppService(bus, client, storage, objects, settings)
            # 把 storage 加载的白名单推到 monitor
            subscribed = await storage.list_subscribed_channels()
            monitor.set_whitelist(c.id for c in subscribed)
            return app_svc, monitor

        # 在 background loop 上跑 setup_async
        setup_fut = asyncio.run_coroutine_threadsafe(setup_async(), qloop)
        app_svc, monitor = setup_fut.result(timeout=5.0)

        # 构造 VM
        vm = MonitorViewModel(app_svc, monitor, qloop)
        # 触发 bootstrap_ui → 内部 fire-and-forget _go()
        vm.bootstrap_ui()

        # 在 background loop 上 wait_for known_channels 填入 3 个
        def _all_three() -> bool:
            return len(vm.known_channels) >= 3

        ok = _wait_for_sync(qloop, _all_three, timeout=3.0)
        assert ok, (
            f"VM.bootstrap_ui 没有在 3s 内填 known_channels;"
            f"got known_channels={dict(vm.known_channels)}; "
            f"client._state={client._state!r}"
        )
        assert len(vm.known_channels) == 3

        # _refresh_state 等价:求 known_channels ∩ subscribed_ids
        subscribed_ids = monitor.subscribed_ids
        rendered_subscribed = [
            ch for cid, ch in vm.known_channels.items()
            if cid in subscribed_ids
        ]
        assert sorted(c.id for c in rendered_subscribed) == [100, 200]


def test_list_joined_waits_for_ready_state_during_transition(qapp, qloop):
    """如果 bridge 还在中间态(tdlib_parameters 等),`list_joined_channels`
    不应"一上来"判 not ready 就 [],而应等最多 N 秒让 state 走到 ready。

    这是 2026-07-18 用户报的"已监听 + 已加入频道在登录状态下打开应用
    的时候未显示"的疑似根因:
      - aiotdlib 触发 updateAuthorizationState(WaitTdlibParameters / ... /
        Ready) 一系列事件后才最终到 Ready;
      - `start()` await 的 `_state_event.wait()` 任何状态变化都 set,
        所以 start() 可能在 WaitTdlibParameters 就返,state 不是 "ready";
      - VM.bootstrap_ui 紧接着 fire-and-forget 调 list_joined_channels;
      - guard 看到 state != "ready" → 立即 [] — 错过稍后才到的 ready,
        channels 永不显示,直到用户手动刷新。

    测试:
      1. VM.bootstrap_ui 时 client._state == "tdlib_parameters"
         (中间态,模拟 aiotdlib 还没走完)
      2. 200ms 后,_state 跳到 "ready"
      3. 等 ≤ 2s,known_channels 应填入 2 个频道 — 不是空
    """
    import tempfile
    import threading as _th
    from pathlib import Path

    from tests.conftest import InMemoryRepository
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, Settings
    from tgmonitor.core.objectstore.local_store import LocalObjectStore

    with tempfile.TemporaryDirectory() as td:
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            api_id=1, api_hash="x" * 32, phone="+8612345",
            session_dir=Path(td) / "s",
            db_root=Path(td) / "m",
            objectstore_root=Path(td) / "o",
            media_policy=MediaPolicy.METADATA,
            db_backend=DBBackend.JSONL,
            objectstore_backend=ObjectStoreBackend.LOCAL,
        )
        settings.ensure_dirs()

        bus = EventBus()
        client = FakeTelegramClient()
        client._state = "tdlib_parameters"  # 中间态
        for cid, title in [(100, "新闻"), (200, "技术")]:
            client.add_channel(ChannelDTO(id=cid, title=title))

        async def setup_async():
            storage = InMemoryRepository()
            await storage.connect()
            objects = LocalObjectStore(root=Path(td) / "o")
            await objects.connect()
            monitor = MonitorService(bus, client, storage, objects, settings)
            app_svc = AppService(bus, client, storage, objects, settings)
            return app_svc, monitor

        setup_fut = asyncio.run_coroutine_threadsafe(setup_async(), qloop)
        app_svc, monitor = setup_fut.result(timeout=5.0)

        vm = MonitorViewModel(app_svc, monitor, qloop)

        # 200ms 后,模拟 aiotdlib 推到 ready
        _th.Timer(0.2, lambda: setattr(client, "_state", "ready")).start()

        vm.bootstrap_ui()

        # 等 ≤ 2s 让 known_channels 填上 2 个
        def _two_channels() -> bool:
            return len(vm.known_channels) >= 2

        ok = _wait_for_sync(qloop, _two_channels, timeout=2.0)
        assert ok, (
            f"list_joined_channels 应等 bridge 走到 ready 再拉,而不是 race "
            f"在中间态就 [] 退出。known_channels={dict(vm.known_channels)}; "
            f"client._state={client._state!r}"
        )


def test_list_joined_waits_for_state_to_become_ready_via_tdlib_client(
    qapp, qloop, tmp_path, stub_aiotdlib_init,
):
    """直接打 TdlibTelegramClient.list_joined_channels:生产代码确实有
    `_state != "ready"` 早返 guard,但 fire-and-forget 调用时机可能正撞
    上 aiotdlib 的 "WaitTdlibParameters → Ready" 序列中间态。

    如果 list_joined_channels 在 _state 不是 ready 时立即 [],bootstrap_ui
    race 时机下永远拿不到 channels。

    修复方向:list_joined_channels 应最多等 N 秒让 _state 走到 "ready",
    再决定 early return / 真请求。

    注意:TdlibTelegramClient 创建(含 asyncio.Event)必须跑在 background
    loop(qloop)上,避免 Python 3.9 下 Event loop 绑定错误
    ("attached to a different loop")。
    """
    from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, Settings
    from tgmonitor.core.events import EventBus
    from tgmonitor.core.telegram import tdlib_client as tdc

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_id=1, api_hash="x" * 32, phone="+8612345",
        session_dir=tmp_path / "session",
        db_root=tmp_path / "m",
        objectstore_root=tmp_path / "o",
        media_policy=MediaPolicy.METADATA,
        db_backend=DBBackend.JSONL,
        objectstore_backend=ObjectStoreBackend.LOCAL,
    )
    bus = EventBus()
    captured = {"called": False}

    async def _setup_and_go():
        """在 background loop 上一气呵成:构造 client → 设中间态 → 模拟 request
        → 0.3s 后推到 ready → list_joined_channels。"""
        client = tdc.TdlibTelegramClient(settings, event_bus=bus)
        client._state = "tdlib_parameters"  # 中间态

        class _R:
            chat_ids: list = [42, 99]

        async def _fake_request(req):  # noqa: ARG001
            captured["called"] = True
            return _R()

        client.request = _fake_request  # type: ignore[method-assign]

        # 0.3s 后在_同一个_ loop 上把 state 推到 ready — 必须用 ensure_future
        # (而非跨线程 Timer),否则 polling 协程读不到另一个线程的 setattr 结果
        # 注意:必须走 _set_state 而非 setattr,因为 _wait_for_state 也依赖
        # _state_event 状态来决定用 sleep(0.05) 还是 wait_for(event.wait()) 路径。
        async def _delay_ready():
            import asyncio as _asyncio
            await _asyncio.sleep(0.3)
            client._set_state("ready")

        asyncio.ensure_future(_delay_ready())

        return await client.list_joined_channels()

    fut = asyncio.run_coroutine_threadsafe(_setup_and_go(), qloop)
    fut.result(timeout=5.0)
    # 如果 list_joined_channels 是 fire-and-forget "先看 state, 非 ready 立即 []",
    # 那 _setup_and_go 会立即返回 [] 且 _fake_request 永远不会被调
    assert captured["called"], (
        "list_joined_channels 没有等到 state 走到 ready,而是在中间态就 []"
    )


def test_wait_for_state_does_not_spin_when_event_already_set(
    qapp, qloop, tmp_path, stub_aiotdlib_init,
):
    """`_state_event` 是 set-only — 一旦被前面的 `_set_state(...)` set 住,
    后续 `wait()` 立即返回,不等 CPU。如果 `_wait_for_state` 用纯
    `wait_for(state_event.wait(), ...)` polling,**没让出 CPU**,qasync
    loop 8s 被 peg 满,Qt 事件无法 pump,UI 冻死(2026-07-18 17:17 用户报)。

    验证两条路径同时成立:
      1) "state 已到 target" 时立即 return(不做 8s 等)
      2) "state 永远不变" 时**让出 CPU**给 qasync loop,8s 内退出(而不是
         spin 至死)。测试方法:用一个**伴随**的 ping 协程同时跑,如果
         `_wait_for_state` 在 spin,ping 不会被 pump;如果 sleep 让出 CPU,
         ping 会被 pump。

    注意:TdlibTelegramClient 创建必须在 background loop 上完成(见前一个
    test 的说明),故全部逻辑包在 `_run` 协程内。
    """
    import time as _t

    from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, Settings
    from tgmonitor.core.events import EventBus
    from tgmonitor.core.telegram import tdlib_client as tdc

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, api_id=1, api_hash="x" * 32, phone="+8612345",
        session_dir=tmp_path / "session",
        db_root=tmp_path / "m",
        objectstore_root=tmp_path / "o",
        media_policy=MediaPolicy.METADATA,
        db_backend=DBBackend.JSONL,
        objectstore_backend=ObjectStoreBackend.LOCAL,
    )
    bus = EventBus()

    async def _run():
        client = tdc.TdlibTelegramClient(settings, event_bus=bus)
        # state 永远停在 tdlib_parameters(≠ ready)。但 event 已经被前面的
        # _set_state(...) 隐式 set 至少一次(任何构造路径都不可能保持 clear)
        client._state = "tdlib_parameters"
        # 强制模拟 event set:
        client._state_event.set()

        async def _ping_loop() -> int:
            """伴随协程:每 ~50ms 记一次 tick,验证 qasync loop 没被 _wait_for_state peg 死。"""
            ticks = 0
            deadline = _t.monotonic() + 6.0
            while _t.monotonic() < deadline:
                await asyncio.sleep(0.05)
                ticks += 1
            return ticks

        async def _wait() -> float:
            t0 = _t.monotonic()
            try:
                await client._wait_for_state("ready", timeout=2.0)
            except TimeoutError:
                pass
            return _t.monotonic() - t0

        # 两个协程同时跑;如果 _wait 是 hot-spin,ping 完全没机会 tick(< 10 ticks)
        # 如果 _wait sleep-yield CPU,ping 应能 tick 50+
        wait_task = asyncio.ensure_future(_wait())
        ping_task = asyncio.ensure_future(_ping_loop())

        elapsed = await asyncio.wait_for(wait_task, timeout=3.0)
        ticks = await asyncio.wait_for(ping_task, timeout=8.0)

        # _wait_for_state 必须 ≤ 2.5s 内退出(不要 spin 死)
        assert elapsed <= 2.5, (
            f"_wait_for_state 总耗时 {elapsed:.2f}s,期望 ≤ 2.5s — "
            f"spin 的话会远超 timeout(应 2s 退)"
        )
        # 6s 周期,50ms 间隔 → 理论 ~120 ticks;最少应能 80+(spin 时 0~2)
        assert ticks >= 40, (
            f"ping 协程只 tick {ticks} 次 — 说明 qasync loop 被 _wait_for_state "
            f"peg 死,UI 会卡住。期望 ≥ 40 ticks(每 50ms 一次)。"
        )

    fut = asyncio.run_coroutine_threadsafe(_run(), qloop)
    fut.result(timeout=15.0)


def test_main_window_initial_refresh_state_is_empty(qapp, qloop):
    """Initial:MainWindow.__init__ 完时,如果 VM 没数据,_refresh_state 应
    渲染空集而不是 NoReturnError 或 stale 数据。
    """
    import tempfile
    from pathlib import Path

    from tests.conftest import InMemoryRepository
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, Settings
    from tgmonitor.core.objectstore.local_store import LocalObjectStore
    from tgmonitor.ui.main_window import MainWindow

    with tempfile.TemporaryDirectory() as td:
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            api_id=1, api_hash="x" * 32, phone="+8612345",
            session_dir=Path(td) / "s",
            db_root=Path(td) / "m",
            objectstore_root=Path(td) / "o",
            media_policy=MediaPolicy.METADATA,
            db_backend=DBBackend.JSONL,
            objectstore_backend=ObjectStoreBackend.LOCAL,
        )
        settings.ensure_dirs()

        bus = EventBus()
        client = FakeTelegramClient()
        client._state = "ready"
        storage = InMemoryRepository()
        objects = LocalObjectStore(root=Path(td) / "o")

        # 在 background loop 上完成 async setup
        async def setup_async():
            await storage.connect()
            await objects.connect()
            monitor = MonitorService(bus, client, storage, objects, settings)
            app_svc = AppService(bus, client, storage, objects, settings)
            return app_svc, monitor

        fut = asyncio.run_coroutine_threadsafe(setup_async(), qloop)
        app_svc, monitor = fut.result(timeout=5.0)

        # MainWindow 构造会触发 __init__ 里的 _refresh_state + bootstrap_ui
        win = MainWindow(app_svc, monitor, qloop, env_path=Path(td) / ".env")
        # initial state:已知频道为空,标签应是 "0"
        assert win.channel_panel.lbl_joined_count.text() == "已加入频道 · 0"
        assert win.channel_panel.lst_joined.count() == 0
