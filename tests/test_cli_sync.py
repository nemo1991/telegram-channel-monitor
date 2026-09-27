"""CLI `sync` / `monitor` 子命令逻辑测试 — 用 conftest fixtures 全 offline。

2026-09-27 refactor:`cli.sync.run_sync` 与 `cli.monitor.run_monitor` 复用
现有 `AppService` / `MonitorService` / `EventBus`,全 offline(zero network),
在 FakeTelegramClient + InMemoryRepository + LocalObjectStore 上跑通。

注:本文件**不**测 `cli.main.main()` 的 argv 路由(那是 `test_cli.py` 的事),
也不测实际 libtdjson / 数据库连接(那是 integration 测试)。

CLI 子包「不加载 PySide6 / qasync」的架构守门测试在 `tests/test_cli.py` 的
`test_cli_does_not_import_qt_or_qasync`(subprocess 隔离,不被 conftest 的
`qapp` fixture 影响)。
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import ChannelSyncProgress
from tgmonitor.core.telegram.fake_client import FakeTelegramClient


def _make_args(**overrides) -> argparse.Namespace:
    """构造 SimpleNamespace 风格的 CLI 参数(覆盖 run_sync 读的所有属性)。"""
    defaults = dict(
        channel_ids=[100],
        no_metadata=False,
        no_history=False,
        resume=False,
        media_policy="metadata",
        chat_delay_ms=0,
        page_delay_ms=0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _setup_ready_client_with_history(client: FakeTelegramClient) -> None:
    """把 fake client 推到 ready + 注入频道元数据 + 历史。"""
    client._state = "ready"
    client.add_channel(ChannelDTO(id=100, title="TestChannel", username="t"))
    client.set_history(100, max_id=5, count=3)  # 5 条历史(mid 1..5)


# ============================================================
# `cli.sync.run_sync`
# ============================================================


async def test_cli_sync_runs_with_fakes(
    app, monitor, bus, storage, client: FakeTelegramClient
) -> None:
    """CLI sync 路径:在 fakes 上跑完一轮 sync,产生进度事件,返回 0。

    注入 `app` / `monitor` 走「own_bootstrap=False」路径,绕开真实 bootstrap
    (需要 libtdjson .dylib / 数据库连接)→ 全 offline。
    """
    from tgmonitor.cli.sync import run_sync

    _setup_ready_client_with_history(client)
    # storage 准备好:cli.sync 走显式 channel_ids,但 sync_channels 内部会
    # upsert_channel_metadata(channel_id, ...) — 需要 storage 真存一份。
    await storage.upsert_channel_metadata(client._channels[100])

    progress_events: list[ChannelSyncProgress] = []

    async def _capture(e: ChannelSyncProgress) -> None:
        progress_events.append(e)

    bus.subscribe(ChannelSyncProgress, _capture)

    args = _make_args(channel_ids=[100])
    rc = await run_sync(args, app=app, monitor=monitor)
    assert rc == 0
    # 至少有一个 "done" 阶段事件
    assert any(e.stage == "done" for e in progress_events), (
        f"expected done stage, got: {[(e.stage, e.channel_id) for e in progress_events]}"
    )


async def test_cli_sync_handles_not_ready(app, monitor, monkeypatch) -> None:
    """bootstrap 返 state != ready 时退出码 1,不调 sync_channels。

    注入 `app` / `monitor`,只 monkeypatch `app.bootstrap` 返 phone_required,
    验证 CLI sync 守门逻辑。
    """
    from tgmonitor.cli.sync import run_sync

    async def fake_bootstrap():
        return ("phone_required", "no session")

    monkeypatch.setattr(app, "bootstrap", fake_bootstrap)
    sync_called = False

    async def fake_sync(*_args, **_kwargs):
        nonlocal sync_called
        sync_called = True
        return None

    monkeypatch.setattr(app, "sync_channels", fake_sync)

    args = _make_args(channel_ids=[100])
    rc = await run_sync(args, app=app, monitor=monitor)
    assert rc == 1
    assert not sync_called, "sync_channels 不应在 bootstrap 未 ready 时被调"


async def test_cli_sync_passes_correct_options(
    app, monitor, bus, storage, client: FakeTelegramClient, monkeypatch
) -> None:
    """CLI sync 把 --no-metadata / --resume / --chat-delay-ms 透传到 SyncOptions。

    monkeypatch `sync_channels` 截获 options 验证字段透传。
    """
    from tgmonitor.cli.sync import run_sync
    from tgmonitor.core.dto import SyncResult

    _setup_ready_client_with_history(client)
    await storage.upsert_channel_metadata(client._channels[100])

    captured_options: list = []

    async def capture_sync(_channel_ids, options):
        captured_options.append(options)
        return SyncResult(per_channel={})

    monkeypatch.setattr(app, "sync_channels", capture_sync)

    args = _make_args(
        channel_ids=[200],
        no_metadata=True,
        resume=True,
        chat_delay_ms=123,
        page_delay_ms=456,
        media_policy="thumbnail",
    )
    rc = await run_sync(args, app=app, monitor=monitor)
    assert rc == 0
    assert len(captured_options) == 1
    opts = captured_options[0]
    assert opts.include_metadata is False  # --no-metadata
    assert opts.resume_from_saved is True  # --resume
    assert opts.chat_delay_ms == 123
    assert opts.page_delay_ms == 456
    # --media-policy 通过 ChannelSyncService.media_policy 生效(SyncOptions 不含该字段)
    assert app.channel_sync.media_policy.value == "thumbnail"


# ============================================================
# `cli.monitor.run_monitor`
# ============================================================


async def test_cli_monitor_handles_not_ready(app, monitor, monkeypatch) -> None:
    """bootstrap 返 state != ready 时退出码 1,不调 monitor.start。

    注入 `app` / `monitor`,monkeypatch `app.bootstrap` 返 phone_required,
    验证 CLI monitor 守门逻辑。
    """
    from tgmonitor.cli.monitor import run_monitor

    async def fake_bootstrap():
        return ("phone_required", "no session")

    monkeypatch.setattr(app, "bootstrap", fake_bootstrap)
    started = False

    async def fake_start():
        nonlocal started
        started = True

    monkeypatch.setattr(monitor, "start", fake_start)

    args = SimpleNamespace(no_resume=False)
    rc = await run_monitor(args, app=app, monitor=monitor)
    assert rc == 1
    assert not started