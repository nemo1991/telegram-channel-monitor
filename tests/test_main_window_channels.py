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
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# 跟 conftest 同步,避免每次新 test 都 inline import。
# 这些在 `test_main_window_initial_refresh_state_is_empty` 等都曾 inline,过
# 不了我新加的 sibling test(函数 scope 不共享)— 现在从 conftest 取一次。
from tgmonitor.core.config import Settings
from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import EventBus
from tgmonitor.core.monitor.service import MonitorService


def _window_settings(base: Path) -> Settings:
    """MainWindow 测试专用 Settings — 3 paths 在 base 下。

    11 处相同 3 行样板(td/tmp_path 都用 3 paths),
    收敛到 1 行调用 — PR cleanup 2026-09-18。
    """
    return Settings.for_test(
        session_dir=base / "s",
        db_root=base / "m",
        objectstore_root=base / "o",
    )


def _build_setup(
    storage,
    objects,
    bus,
    client,
    settings,
    *,
    before_monitor=None,
    after_monitor=None,
):
    """2026-10-02 v1.12.1 抽出:`test_main_window_initial_refresh_state_is_empty`
    不复用 qloop fixture,改用一次性临时 loop,避免 _LoopThread 跨 test 残留。
    返回 awaitable,让调用方决定用哪个 loop 跑。

    2026-10-03 v1.12.1 follow-up:扩 hooks(`before_monitor` 在 storage.connect
    之后、MonitorService 创建之前;`after_monitor` 在 MonitorService 创建之后、
    返回前)— 让 line 132/209 这种需要在 monitor 创建前后做 storage.seed 或
    set_whitelist 的 test 也能用 temp_loop 模式跑 setup_async。
    """
    from tgmonitor.core.app_service import AppService  # noqa: PLC0415

    async def _go():
        await storage.connect()
        await objects.connect()
        if before_monitor is not None:
            await before_monitor(storage)
        monitor = MonitorService(bus, client, storage, objects, settings)
        app_svc = AppService(bus, client, storage, objects, settings)
        if after_monitor is not None:
            await after_monitor(storage, monitor)
        return app_svc, monitor

    return _go()


# 2026-11-14:删掉本地 function-scope `qloop`,统一用
# `tests.fixtures._qt_runtime` 的 session-scope 共享 loop(全会话只 1 个
# 后台线程)。测试函数参数名 `qloop` 保持不变。


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
            # 2026-11-13:0.02s 的 step 在 CI runner 上几乎必然超时,原实现
            # 既不 cancel 也不等后台 loop 执行,_check() coroutine 会堆在
            # ready queue 里变成 "never awaited" 垃圾,污染后续 test 的
            # qloop 调度时序。撤掉 future 并让出主线程,每次只留 1 个。
            fut.cancel()
            ok = False
        if ok:
            return True
        remaining = deadline - _t.monotonic()
        if remaining <= 0:
            break
        _t.sleep(min(step, remaining))
    return False


def test_list_joined_waits_for_state_to_become_ready_via_telethon_client(
    qapp,
    tmp_path,
):
    """直接打 TelethonTelegramClient.list_joined_channels:生产代码确实有
    `_state != "ready"` 早返 guard,但 fire-and-forget 调用时机可能正撞
    上启动中间态。

    如果 list_joined_channels 在 _state 不是 ready 时立即 [],bootstrap_ui
    race 时机下永远拿不到 channels。

    修复方向:list_joined_channels 应最多等 N 秒让 _state 走到 "ready",
    再决定 early return / 真请求。

    2026-09-29 备注:VM.bootstrap_ui 已不再用 list_joined_channels(改走
    storage 真理),但 `list_joined_channels` 本身仍被 live_forward dialog
    (`MainWindow._on_live_forward`)调用 —— race 守护仍要保留。

    v2.0.0:从 TdlibTelegramClient 迁到 TelethonTelegramClient(mock 后端,
    iter_dialogs 只记录「被调到」)。

    2026-11-13:本用例是全套裸跑下唯一稳定偶发红的用例(macOS runner 连续
    两次 `fut.result(timeout=5.0)` TimeoutError + teardown 报
    `LoopThread cleanup timeout: thread still alive after 10.0s — daemon leak`),
    单独跑 5 次全过 → 跨 test 的 qloop 残留污染,不是生产代码 bug。
    沿用本文件已有 3 处 `temp_loop` 先例:setup 完全自包含(不碰 Qt /
    不跨线程),改走一次性临时 loop;并用 `wait_for` 做总超时,保证超时时
    连伴随的 `_delay_ready` task 一起 cancel,不留 pending。
    """
    from unittest.mock import MagicMock

    from tgmonitor.core.telegram.telethon_client import TelethonTelegramClient

    settings = _window_settings(tmp_path)
    bus = EventBus()
    captured = {"called": False}

    async def _setup_and_go():
        """在 background loop 上一气呵成:构造 client → 中间态 → 0.3s 后推到
        ready → list_joined_channels。"""
        client = TelethonTelegramClient(settings, event_bus=bus)
        # 初始 state="uninit"(中间态);_client 装 mock,iter_dialogs 记录调用

        async def _dialogs():
            captured["called"] = True
            return
            yield  # 空 dialog 流 — 只验证「被调到」

        mock = MagicMock()
        mock.iter_dialogs = _dialogs
        client._client = mock

        # 0.3s 后在_同一个_ loop 上把 state 推到 ready — 必须用 ensure_future
        # (而非跨线程 Timer),否则 polling 协程读不到另一个线程的 setattr 结果
        # 注意:必须走 _set_state 而非 setattr,因为 _wait_for_state 也依赖
        # _state_event 状态来决定用 sleep(0.05) 还是 wait_for(event.wait()) 路径。
        async def _delay_ready():
            await asyncio.sleep(0.3)
            client._set_state("ready")

        ready_task = asyncio.ensure_future(_delay_ready())
        try:
            return await client.list_joined_channels()
        finally:
            # list_joined_channels 若提前返回,_delay_ready 仍是 pending;
            # 显式 cancel + await 收掉,避免 close loop 时留 "Task was destroyed"。
            ready_task.cancel()
            try:
                await ready_task
            except asyncio.CancelledError:
                pass

    # 一次性临时 loop:不依赖 qloop fixture,`wait_for` 超时会连
    # _setup_and_go 一起 cancel,不会把残留 task 留给后续 test。
    temp_loop = asyncio.new_event_loop()
    try:
        temp_loop.run_until_complete(asyncio.wait_for(_setup_and_go(), timeout=5.0))
    finally:
        if not temp_loop.is_closed():
            temp_loop.close()
    # 如果 list_joined_channels 是 fire-and-forget "先看 state, 非 ready 立即 []",
    # 那 _setup_and_go 会立即返回 [] 且 iter_dialogs 永远不会被调
    assert captured["called"], "list_joined_channels 没有等到 state 走到 ready,而是在中间态就 []"


# ============================================================
# ChannelWidget 新用户空状态:已加入列表为空时显示引导,有数据自动隐藏
# ============================================================


# ============================================================
# ChannelListCard — 抽出后的 reusable card helper
# ============================================================


def test_channel_list_card_signals_emit_on_action_and_row_button(qapp):
    """v2.0.7 FR-07:action_clicked / 行尾按钮 signal 触发,64 位 id 不溢出。"""
    from PySide6.QtWidgets import QToolButton

    from tgmonitor.ui.widgets.channel_widget import ChannelListCard

    card = ChannelListCard(title="x", action_label="act", row_action_icon="plus")
    action_calls: list[int] = []
    card.action_clicked.connect(lambda: action_calls.append(1))
    card.btn_action.click()
    assert action_calls == [1]

    big_id = -1001375475051  # 真实 Telegram chat_id,32 位 Signal 会溢出
    card.set_items([ChannelDTO(id=big_id, title="x", kind="channel")], count_template="{n}")
    actioned_ids: list[int] = []
    card.item_action_clicked.connect(actioned_ids.append)
    row = card.lst.itemWidget(card.lst.item(0))
    assert row is not None
    btn = row.findChild(QToolButton, "channelRowAction")
    assert btn is not None
    btn.click()
    assert actioned_ids == [big_id]
    assert not hasattr(card, "item_double_clicked")
    card.deleteLater()


# ============================================================
# 2026-11-19 hotfix:双击加入监听后「已加入频道」左栏被塌缩成已监听
# ============================================================
