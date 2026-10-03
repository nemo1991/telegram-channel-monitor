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
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt  # noqa: E402

# 跟 conftest 同步,避免每次新 test 都 inline import。
# 这些在 `test_main_window_initial_refresh_state_is_empty` 等都曾 inline,过
# 不了我新加的 sibling test(函数 scope 不共享)— 现在从 conftest 取一次。
from tests.conftest import InMemoryRepository
from tgmonitor.core.config import Settings
from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import EventBus
from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.core.telegram.fake_client import FakeTelegramClient
from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel

# `stub_tdlib_init` fixture 由 tests/conftest.py 统一提供


def _window_settings(base: Path) -> Settings:
    """MainWindow 测试专用 Settings — phone="+8612345" + 3 paths 在 base 下。

    11 处相同 4 行样板(td/tmp_path 都用 phone="+8612345" + 3 paths),
    收敛到 1 行调用 — PR cleanup 2026-09-18。
    """
    return Settings.for_test(
        phone="+8612345",
        session_dir=base / "s",
        db_root=base / "m",
        objectstore_root=base / "o",
    )


class _LoopThread:
    """历史兼容 wrapper — 2026-10-03 v1.12.1 把 inline 定义集中到
    `tests.fixtures._loop_thread.LoopThread`,旧 API(`lt.loop` /
    `lt.asyncio_loop`)保留,新测试用 `LoopThread` 直接。
    """

    def __init__(self) -> None:
        from tests.fixtures._loop_thread import LoopThread

        self._inner = LoopThread()
        self.loop = self._inner.loop
        self._thread = self._inner._thread

    def _run(self) -> None:
        # 占位 — 实际跑在 LoopThread._run
        return None

    def stop(self) -> None:
        self._inner.stop()

    @property
    def asyncio_loop(self) -> asyncio.AbstractEventLoop:
        return self.loop


def _build_setup(storage, objects, bus, client, settings):
    """2026-10-02 v1.12.1 抽出:`test_main_window_initial_refresh_state_is_empty`
    不复用 qloop fixture,改用一次性临时 loop,避免 _LoopThread 跨 test 残留。
    返回 awaitable,让调用方决定用哪个 loop 跑。
    """
    from tgmonitor.core.app_service import AppService  # noqa: PLC0415

    async def _go():
        await storage.connect()
        await objects.connect()
        monitor = MonitorService(bus, client, storage, objects, settings)
        app_svc = AppService(bus, client, storage, objects, settings)
        return app_svc, monitor

    return _go()


@pytest.fixture
def qloop() -> _LoopThread:
    """后台线程 + run_forever loop — 模拟 qasync 主线程 loop。

    2026-10-03 v1.12.1:cleanup 改走 `LoopThread.stop()`,统一 cancel → drain
    → stop → join → close,跨 test 不 leak 残留 thread/loop。
    """
    lt = _LoopThread()
    try:
        yield lt.loop
    finally:
        lt.stop()


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


def test_vm_bootstrap_populates_known_channels_from_storage(qapp, qloop):
    """2026-09-29:VM.bootstrap_ui 改走 storage 真理,不再走 TDLib getChats。

    设计意图:
      - 已订阅真理来自 `storage.list_subscribed_channels()`,不来自
        `client.list_joined_channels()`。
      - 用户 join 了 1000 个频道但只白名单 5 个时,Media Manager 「全部频道」
        下拉只显示 5 个,不会出现 1000 项噪音。
      - VM bootstrap 不再被 TDLib state race 影响 — storage 是同步真理,
        TDLib 还在中间态也能立刻拉到白名单。

    测试:
      1) client.add_channel 注入 3 个频道(TD 视角)
      2) storage.upsert_channel 注入 2 个 is_subscribed=True(真理视角)
      3) VM.bootstrap_ui 触发后,known_channels 应等于 2(只真理),
         不是 3——反映新语义。
    """
    import tempfile
    from pathlib import Path

    from tests.conftest import InMemoryRepository
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.objectstore.local_store import LocalObjectStore

    with tempfile.TemporaryDirectory() as td:
        settings = _window_settings(Path(td))

        bus = EventBus()
        client = FakeTelegramClient()
        # 推到 "ready" 状态:本测试里直接赋属性(避免跑完整 login flow)
        client._state = "ready"
        # 注入 3 个已加入的频道(TD 视角,大于白名单)
        for cid, title in [(100, "新闻"), (200, "技术"), (300, "财经")]:
            client.add_channel(ChannelDTO(id=cid, title=title))

        async def setup_async() -> tuple:
            storage = InMemoryRepository()
            await storage.connect()
            # 真理侧:只 100、200 is_subscribed=True;300 只在 TD 那边存在
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
        app_svc, monitor = setup_fut.result(timeout=10.0)

        # 构造 VM
        vm = MonitorViewModel(app_svc, monitor, qloop)
        # 触发 bootstrap_ui → 内部 fire-and-forget _go()
        vm.bootstrap_ui()

        # 等 known_channels 填入 2 个(只真理)
        def _two_channels() -> bool:
            return len(vm.known_channels) >= 2

        ok = _wait_for_sync(qloop, _two_channels, timeout=3.0)
        assert ok, (
            f"VM.bootstrap_ui 没有在 3s 内填 known_channels;"
            f"got known_channels={dict(vm.known_channels)}"
        )
        # 新语义:VM 走 storage,只看到 2 个 subscribed(不是 3 个 TD 加入)
        assert len(vm.known_channels) == 2
        assert sorted(vm.known_channels.keys()) == [100, 200]
        # 300 不应出现 —— TD join 了但 storage 没订阅,真理优先
        assert 300 not in vm.known_channels


def test_vm_bootstrap_does_not_wait_for_tdlib_state(qapp, qloop):
    """2026-09-29:VM bootstrap 走 storage,不被 TDLib state race 阻塞。

    旧 race(已被消除):VM.bootstrap_ui 紧接着 fire-and-forget 调
    `client.list_joined_channels()`;`list_joined_channels` 在 client._state
    != "ready" 时立即 [] 退出 → 中间态窗口里 channels 永不显示。

    新设计:storage 是同步真理,VM bootstrap 直接读,跟 TDLib state 解耦。

    测试:
      1) client._state = 'tdlib_parameters'(中间态,模拟 TDLib 还没走完)
      2) storage 里 2 个 subscribed channels(真理已就绪)
      3) VM.bootstrap_ui 触发后,**不等 state 变 ready**,known_channels
         立刻填上 2 个 —— storage 不需要 TDLib state。
    """
    import tempfile
    from pathlib import Path

    from tests.conftest import InMemoryRepository
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.objectstore.local_store import LocalObjectStore

    with tempfile.TemporaryDirectory() as td:
        settings = _window_settings(Path(td))

        bus = EventBus()
        client = FakeTelegramClient()
        client._state = "tdlib_parameters"  # 中间态 —— 不动它
        # TD 视角故意为空 / 不一致:VM 不应该看
        # (如果走 list_joined 会拿到 [],但我们不在 client 上 add_channel)

        async def setup_async():
            storage = InMemoryRepository()
            await storage.connect()
            # 真理侧 2 个 subscribed
            await storage.upsert_channel(ChannelDTO(id=100, title="新闻", is_subscribed=True))
            await storage.upsert_channel(ChannelDTO(id=200, title="技术", is_subscribed=True))

            objects = LocalObjectStore(root=Path(td) / "o")
            await objects.connect()
            monitor = MonitorService(bus, client, storage, objects, settings)
            app_svc = AppService(bus, client, storage, objects, settings)
            return app_svc, monitor

        setup_fut = asyncio.run_coroutine_threadsafe(setup_async(), qloop)
        app_svc, monitor = setup_fut.result(timeout=10.0)

        vm = MonitorViewModel(app_svc, monitor, qloop)
        vm.bootstrap_ui()

        # 给 0.5s —— storage 是同步真理,500ms 应该填好(测试也不让 wait 太长)
        def _two_channels() -> bool:
            return len(vm.known_channels) >= 2

        ok = _wait_for_sync(qloop, _two_channels, timeout=0.5)
        assert ok, (
            f"VM.bootstrap_ui 应该不依赖 TDLib state;但 1s 内没填上 channels。"
            f"known_channels={dict(vm.known_channels)};"
            f"client._state={client._state!r}"
        )
        # 关键断言:client 仍然是中间态,但 VM 已经填好
        assert client._state == "tdlib_parameters"
        assert sorted(vm.known_channels.keys()) == [100, 200]


def test_list_joined_waits_for_state_to_become_ready_via_tdlib_client(
    qapp,
    qloop,
    tmp_path,
    stub_tdlib_init,
):
    """直接打 TdlibTelegramClient.list_joined_channels:生产代码确实有
    `_state != "ready"` 早返 guard,但 fire-and-forget 调用时机可能正撞
    上 TDLib 的 "WaitTdlibParameters → Ready" 序列中间态。

    如果 list_joined_channels 在 _state 不是 ready 时立即 [],bootstrap_ui
    race 时机下永远拿不到 channels。

    修复方向:list_joined_channels 应最多等 N 秒让 _state 走到 "ready",
    再决定 early return / 真请求。

    2026-09-29 备注:VM.bootstrap_ui 已不再用 list_joined_channels(改走
    storage 真理),但 `list_joined_channels` 本身仍被 live_forward dialog
    (`MainWindow._on_live_forward`,main_window.py:1171)调用 —— race
    守护仍要保留。

    注意:TdlibTelegramClient 创建(含 asyncio.Event)必须跑在 background
    loop(qloop)上,避免 Python 3.9 下 Event loop 绑定错误
    ("attached to a different loop")。
    """
    from tgmonitor.core.events import EventBus
    from tgmonitor.core.telegram import tdlib_client as tdc

    settings = _window_settings(tmp_path)
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
    assert captured["called"], "list_joined_channels 没有等到 state 走到 ready,而是在中间态就 []"


def test_wait_for_state_does_not_spin_when_event_already_set(
    qapp,
    qloop,
    tmp_path,
    stub_tdlib_init,
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

    from tgmonitor.core.events import EventBus
    from tgmonitor.core.telegram import tdlib_client as tdc

    settings = _window_settings(tmp_path)
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
            f"_wait_for_state 总耗时 {elapsed:.2f}s,期望 ≤ 2.5s — spin 的话会远超 timeout(应 2s 退)"
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

    2026-10-03 v1.12.1:CI runner (windows 优先) 上 `LoopThread` 后台 loop
    在前一个 channels test cleanup 时残留(`LoopThread.stop()` 的 5s join
    timeout 在 windows runner 不够)→ 第 5 个 test 的
    `run_coroutine_threadsafe(setup_async, qloop)` 把 coro 调度到受污染
    的 qloop → 10s 不返回 TimeoutError。

    改:用一次性临时 loop 跑 setup_async(本 test 自己管理生命周期)—
    不依赖 qloop fixture,避免 `_LoopThread` 跨 test 残留污染。本地行为不变。
    """
    import tempfile
    from pathlib import Path

    from tests.conftest import InMemoryRepository
    from tgmonitor.core.objectstore.local_store import LocalObjectStore
    from tgmonitor.ui.main_window import MainWindow

    with tempfile.TemporaryDirectory() as td:
        settings = _window_settings(Path(td))

        bus = EventBus()
        client = FakeTelegramClient()
        client._state = "ready"
        storage = InMemoryRepository()
        objects = LocalObjectStore(root=Path(td) / "o")

        # 用一次性临时 loop 跑 setup_async(本 test 自己管理生命周期)—
        # 不依赖 qloop fixture,避免 _LoopThread 跨 test 残留污染。
        temp_loop = asyncio.new_event_loop()
        try:
            app_svc, monitor = temp_loop.run_until_complete(
                _build_setup(storage, objects, bus, client, settings)
            )
        finally:
            temp_loop.close()

        # MainWindow 构造会触发 __init__ 里的 _refresh_state + bootstrap_ui
        win = MainWindow(app_svc, monitor, qloop, env_path=Path(td) / ".env")
        # initial state:已知频道为空,标签应是 "0"
        assert win.channel_panel.lbl_joined_count.text() == "已加入频道 · 0"
        assert win.channel_panel.lst_joined.count() == 0


# ============================================================
# ChannelWidget 新用户空状态:已加入列表为空时显示引导,有数据自动隐藏
# ============================================================


def test_channel_widget_empty_joined_visible_when_no_data(qapp, qloop):
    """新用户首启:已加入列表为空 → _empty_joined 应显示(给新用户引导)。"""
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.objectstore.local_store import LocalObjectStore
    from tgmonitor.ui.widgets.channel_widget import ChannelWidget

    with tempfile.TemporaryDirectory() as td:
        settings = _window_settings(Path(td))

        bus = EventBus()
        client = FakeTelegramClient()
        client._state = "ready"
        storage = InMemoryRepository()
        objects = LocalObjectStore(root=Path(td) / "o")

        async def setup_async():
            await storage.connect()
            from tgmonitor.core.monitor.service import MonitorService

            monitor = MonitorService(bus, client, storage, objects, settings)
            app_svc = AppService(bus, client, storage, objects, settings)
            return app_svc, monitor

        fut = asyncio.run_coroutine_threadsafe(setup_async(), qloop)
        app_svc, monitor = fut.result(timeout=10.0)

        widget = ChannelWidget(app_svc, qloop)
        # 构造完没有数据 → _empty_joined 应显示
        assert widget.lst_joined.count() == 0
        assert not widget._empty_joined.isHidden()


def test_channel_widget_empty_joined_hidden_after_set_joined(qapp, qloop):
    """set_joined([...]) 装载数据 → _empty_joined 自动隐藏。"""
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.dto import ChannelDTO
    from tgmonitor.core.objectstore.local_store import LocalObjectStore
    from tgmonitor.ui.widgets.channel_widget import ChannelWidget

    with tempfile.TemporaryDirectory() as td:
        settings = _window_settings(Path(td))

        bus = EventBus()
        client = FakeTelegramClient()
        client._state = "ready"
        storage = InMemoryRepository()
        objects = LocalObjectStore(root=Path(td) / "o")

        async def setup_async():
            await storage.connect()
            from tgmonitor.core.monitor.service import MonitorService

            monitor = MonitorService(bus, client, storage, objects, settings)
            app_svc = AppService(bus, client, storage, objects, settings)
            return app_svc, monitor

        fut = asyncio.run_coroutine_threadsafe(setup_async(), qloop)
        app_svc, monitor = fut.result(timeout=10.0)

        widget = ChannelWidget(app_svc, qloop)
        # 先确认空时显示
        assert not widget._empty_joined.isHidden()

        # 装载一条频道
        widget.set_joined(
            [
                ChannelDTO(id=42, title="新闻频道", kind="channel"),
            ]
        )
        assert widget.lst_joined.count() == 1
        assert widget._empty_joined.isHidden()

        # 清空回 [] → overlay 再出现
        widget.set_joined([])
        assert widget.lst_joined.count() == 0
        assert not widget._empty_joined.isHidden()


# ============================================================
# ChannelListCard — 抽出后的 reusable card helper
# ============================================================


def test_channel_list_card_set_items_sorts_and_sets_icons(qapp):
    """直接测 `ChannelListCard`:set_items 按 title 排序 + kind icon + count label。"""
    from tgmonitor.ui.widgets.channel_widget import ChannelListCard

    card = ChannelListCard(
        title="已加入频道",
        action_label="刷新",
    )
    chs = [
        ChannelDTO(id=2, title="B新闻", kind="channel"),
        ChannelDTO(id=1, title="A新闻", kind="supergroup"),
        ChannelDTO(id=3, title="C新闻", kind="group"),
    ]
    card.set_items(chs, count_template="已加入频道 · {n}")
    assert card.lst.count() == 3
    # 排序:title 字典序 A → B → C
    assert [card.lst.item(i).data(Qt.UserRole) for i in range(3)] == [1, 2, 3]
    assert card.count_label.text() == "已加入频道 · 3"


def test_channel_list_card_clear_items_hides_data(qapp):
    """clear_items 把 list 清空 + count label 重置。"""
    from tgmonitor.ui.widgets.channel_widget import ChannelListCard

    card = ChannelListCard(title="已监听", action_label="同步")
    card.set_items([ChannelDTO(id=1, title="x", kind="channel")], count_template="已监听 · {n}")
    assert card.lst.count() == 1
    card.clear_items(count_template="已监听 · {n}")
    assert card.lst.count() == 0
    assert card.count_label.text() == "已监听 · 0"


def test_channel_list_card_add_remove_item(qapp):
    """add_item 追加,remove_by_cid 移除(返回 bool)。"""
    from tgmonitor.ui.widgets.channel_widget import ChannelListCard

    card = ChannelListCard(title="x", action_label="y")
    card.add_item(ChannelDTO(id=10, title="c10", kind="channel"))
    card.add_item(ChannelDTO(id=20, title="c20", kind="channel"))
    assert card.lst.count() == 2
    assert card.remove_by_cid(10) is True
    assert card.find_cid(10) == -1
    assert card.find_cid(20) == 0
    # 再删一次返 False
    assert card.remove_by_cid(10) is False


def test_channel_list_card_apply_filter(qapp):
    """apply_filter 按 title/username 过滤;空 text = 全显。"""
    from tgmonitor.ui.widgets.channel_widget import ChannelListCard

    card = ChannelListCard(title="x", action_label="y")
    chs = [
        ChannelDTO(id=1, title="Python Daily", kind="channel", username="pydaily"),
        ChannelDTO(id=2, title="Rust Weekly", kind="channel", username="rustw"),
        ChannelDTO(id=3, title="GoLang News", kind="channel"),
    ]
    card.set_items(chs, count_template="{n}")
    mapping = {c.id: c for c in chs}

    # 空 → 全显
    card.apply_filter("", mapping)
    assert all(not card.lst.item(i).isHidden() for i in range(3))

    # "py" → 只匹配 Python Daily
    card.apply_filter("py", mapping)
    visible = [i for i in range(3) if not card.lst.item(i).isHidden()]
    assert visible == [1]  # Python Daily is index 1 (after sort)

    # "weekly" → 只匹配 Rust Weekly
    card.apply_filter("weekly", mapping)
    visible = [i for i in range(3) if not card.lst.item(i).isHidden()]
    assert visible == [2]

    # username 匹配也能命中
    card.apply_filter("pydaily", mapping)
    visible = [i for i in range(3) if not card.lst.item(i).isHidden()]
    assert visible == [1]  # Python Daily is index 1 (alphabetic sort)


def test_channel_list_card_selected_cids_when_no_selection(qapp):
    """默认 SingleSelection + 没选 → selected_cids 返 []。"""
    from tgmonitor.ui.widgets.channel_widget import ChannelListCard

    card = ChannelListCard(title="x", action_label="y")
    card.set_items([ChannelDTO(id=1, title="c1", kind="channel")], count_template="{n}")
    assert card.selected_cids() == []
    assert card.all_cids() == [1]


def test_channel_list_card_extended_selection_mode(qapp):
    """`extended_selection=True` 把 list 切到 ExtendedSelection(用来多选 sync)。"""
    from PySide6.QtWidgets import QAbstractItemView

    from tgmonitor.ui.widgets.channel_widget import ChannelListCard

    card = ChannelListCard(
        title="已监听",
        action_label="同步",
        extended_selection=True,
    )
    assert card.lst.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection


def test_channel_list_card_empty_hint_visibility_toggles(qapp):
    """配置了 empty_hint_spec:数据 0 → hint 显示,有数据 → 隐藏。"""
    from tgmonitor.ui.widgets.channel_widget import ChannelListCard

    card = ChannelListCard(
        title="x",
        action_label="y",
        empty_hint_spec=("💡", "空", "请加数据"),
    )
    assert not card._empty_hint.isHidden()  # 初始:空 → 显示

    card.set_items([ChannelDTO(id=1, title="c", kind="channel")], count_template="{n}")
    assert card._empty_hint.isHidden()  # 有数据 → 隐藏

    card.clear_items(count_template="{n}")
    assert not card._empty_hint.isHidden()  # 清空 → 再显示


def test_channel_list_card_signals_emit_on_action_and_double_click(qapp):
    """action_clicked / item_double_clicked 信号正常触发。"""
    from tgmonitor.ui.widgets.channel_widget import ChannelListCard

    card = ChannelListCard(title="x", action_label="act")
    action_calls: list[int] = []
    card.action_clicked.connect(lambda: action_calls.append(1))
    card.btn_action.click()
    assert action_calls == [1]

    card.set_items([ChannelDTO(id=42, title="x", kind="channel")], count_template="{n}")
    double_clicked_ids: list[int] = []
    card.item_double_clicked.connect(double_clicked_ids.append)
    card.lst.itemDoubleClicked.emit(card.lst.item(0))
    assert double_clicked_ids == [42]  # integer channel_id,not QListWidgetItem


# ============================================================
# MainWindow._on_sync_requested 拆出的 helper
# ============================================================


def test_build_sync_titles_uses_known_channels(qapp, qloop) -> None:
    """`_build_sync_titles(ids)` 从 VM.known_channels 拉 title,缺时回退
    `#<id>`。"""
    import tempfile

    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.objectstore.local_store import LocalObjectStore
    from tgmonitor.ui.main_window import MainWindow

    with tempfile.TemporaryDirectory() as td:
        settings = _window_settings(Path(td))

        bus = EventBus()
        client = FakeTelegramClient()
        client._state = "ready"
        storage = InMemoryRepository()
        objects = LocalObjectStore(root=Path(td) / "o")

        async def setup_async():
            await storage.connect()
            from tgmonitor.core.monitor.service import MonitorService

            monitor = MonitorService(bus, client, storage, objects, settings)
            app_svc = AppService(bus, client, storage, objects, settings)
            return app_svc, monitor

        fut = asyncio.run_coroutine_threadsafe(setup_async(), qloop)
        app_svc, monitor = fut.result(timeout=10.0)

        win = MainWindow(app_svc, monitor, qloop, env_path=Path(td) / ".env")
        # VM 没数据,全部回退到 `#<id>`
        titles = win._build_sync_titles([100, 200, 300])
        assert titles == {100: "#100", 200: "#200", 300: "#300"}


def test_build_sync_titles_uses_vm_dto_when_present(qapp, qloop) -> None:
    """VM.known_channels 有 DTO 时优先用 `.title`,不回退。"""
    import tempfile

    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.objectstore.local_store import LocalObjectStore
    from tgmonitor.ui.main_window import MainWindow

    with tempfile.TemporaryDirectory() as td:
        settings = _window_settings(Path(td))

        bus = EventBus()
        client = FakeTelegramClient()
        client._state = "ready"
        storage = InMemoryRepository()
        objects = LocalObjectStore(root=Path(td) / "o")

        async def setup_async():
            await storage.connect()
            from tgmonitor.core.monitor.service import MonitorService

            monitor = MonitorService(bus, client, storage, objects, settings)
            app_svc = AppService(bus, client, storage, objects, settings)
            return app_svc, monitor

        fut = asyncio.run_coroutine_threadsafe(setup_async(), qloop)
        app_svc, monitor = fut.result(timeout=10.0)

        win = MainWindow(app_svc, monitor, qloop, env_path=Path(td) / ".env")
        # 直接 inject VM.known_channels 一个 DTO
        win._vm.known_channels[42] = ChannelDTO(id=42, title="新闻频道")
        titles = win._build_sync_titles([42, 100])
        assert titles[42] == "新闻频道"
        assert titles[100] == "#100"  # 没 DTO,回退


def test_show_sync_options_dialog_returns_defaults_from_settings(qapp, qloop) -> None:
    """`_show_sync_options_dialog` 默认从 settings 拉 sync_chat_delay_ms 等。

    不直接 exec()(modal 阻塞),而是把 SyncOptionsDialog 替换成 stub 验证
    SyncOptions 默认值正确。

    注:main_window 直接 `from sync_dialog import SyncOptionsDialog`,
    binding 在 main_window namespace,patch 必须在 `main_window.SyncOptionsDialog`
    而非 `sync_dialog.SyncOptionsDialog` 才生效。
    """
    import tempfile

    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.config import Settings
    from tgmonitor.core.dto import SyncOptions
    from tgmonitor.core.objectstore.local_store import LocalObjectStore
    from tgmonitor.ui import main_window as mw

    with tempfile.TemporaryDirectory() as td:
        settings = Settings.for_test(
            phone="+8612345",
            session_dir=Path(td) / "s",
            db_root=Path(td) / "m",
            objectstore_root=Path(td) / "o",
            sync_chat_delay_ms=777,
            sync_page_delay_ms=888,
        )

        bus = EventBus()
        client = FakeTelegramClient()
        client._state = "ready"
        storage = InMemoryRepository()
        objects = LocalObjectStore(root=Path(td) / "o")

        async def setup_async():
            await storage.connect()
            from tgmonitor.core.monitor.service import MonitorService

            monitor = MonitorService(bus, client, storage, objects, settings)
            app_svc = AppService(bus, client, storage, objects, settings)
            return app_svc, monitor

        fut = asyncio.run_coroutine_threadsafe(setup_async(), qloop)
        app_svc, monitor = fut.result(timeout=10.0)

        win = mw.MainWindow(app_svc, monitor, qloop, env_path=Path(td) / ".env")

        # Patch the SyncOptionsDialog class on main_window's namespace
        captured: dict = {}

        class _StubDialog:
            def __init__(self, channel_ids, titles, defaults, parent):
                captured["channel_ids"] = channel_ids
                captured["titles"] = titles
                captured["defaults"] = defaults

            def exec(self) -> int:  # pretend user clicked OK
                return 1  # QDialog.Accepted

            def options(self):
                return captured["defaults"]

        original = mw.SyncOptionsDialog
        mw.SyncOptionsDialog = _StubDialog  # type: ignore[assignment,misc]
        try:
            result = win._show_sync_options_dialog(
                [1, 2, 3],
                {1: "c1", 2: "c2", 3: "c3"},
            )
        finally:
            mw.SyncOptionsDialog = original  # type: ignore[assignment,misc]

        assert isinstance(result, SyncOptions)
        assert result.chat_delay_ms == 777
        assert result.page_delay_ms == 888
        # titles / channel_ids 也透传
        assert captured["channel_ids"] == [1, 2, 3]
        assert captured["titles"] == {1: "c1", 2: "c2", 3: "c3"}


def test_show_sync_options_dialog_returns_none_when_cancelled(qapp, qloop) -> None:
    """用户点取消 → exec() 返 0 → helper 返 None — caller 不继续 sync。"""
    import tempfile

    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.objectstore.local_store import LocalObjectStore
    from tgmonitor.ui import main_window as mw

    with tempfile.TemporaryDirectory() as td:
        settings = _window_settings(Path(td))

        bus = EventBus()
        client = FakeTelegramClient()
        client._state = "ready"
        storage = InMemoryRepository()
        objects = LocalObjectStore(root=Path(td) / "o")

        async def setup_async():
            await storage.connect()
            from tgmonitor.core.monitor.service import MonitorService

            monitor = MonitorService(bus, client, storage, objects, settings)
            app_svc = AppService(bus, client, storage, objects, settings)
            return app_svc, monitor

        fut = asyncio.run_coroutine_threadsafe(setup_async(), qloop)
        app_svc, monitor = fut.result(timeout=10.0)

        win = mw.MainWindow(app_svc, monitor, qloop, env_path=Path(td) / ".env")

        class _StubCancelDialog:
            def __init__(self, *args, **kwargs):
                pass

            def exec(self) -> int:
                return 0  # QDialog.Rejected

            def options(self):
                return None

        original = mw.SyncOptionsDialog
        mw.SyncOptionsDialog = _StubCancelDialog  # type: ignore[assignment,misc]
        try:
            result = win._show_sync_options_dialog([1], {1: "c1"})
        finally:
            mw.SyncOptionsDialog = original  # type: ignore[assignment,misc]

        assert result is None
