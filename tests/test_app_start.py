"""AppService.start() 参数化启动序列单测 — P1-9 / ADR-002 决策 1。

三处调用方(GUI / CLI monitor / CLI sync)已把「白名单 → monitor → bootstrap」
收敛到本入口,这里守护步骤顺序、策略开关与 StartResult 字段。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from tgmonitor.core.app_service import AppService, StartOptions, StartResult
from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import EventBus


def _make_app(
    *, paused: bool = False, with_monitor: bool = True
) -> tuple[AppService, MagicMock | None]:
    bus = EventBus()
    client = AsyncMock()
    client.start.return_value = ("ready", None)
    storage = AsyncMock()
    objects = AsyncMock()
    objects.backend_name = "fake"
    settings = MagicMock()
    settings.media_policy = "full"
    settings.paused = paused
    monitor: MagicMock | None = None
    if with_monitor:
        monitor = MagicMock()
        monitor.start = AsyncMock()
        monitor.set_whitelist = MagicMock()
    app = AppService(bus, client, storage, objects, settings, monitor=monitor)
    return app, monitor


async def test_start_runs_steps_in_fixed_order(monkeypatch) -> None:
    """默认策略:白名单(storage) → monitor → bootstrap,顺序固定。"""
    app, monitor = _make_app()
    assert monitor is not None
    calls: list[object] = []

    async def fake_list():
        calls.append("list_monitored")
        return [
            ChannelDTO(id=1, title="A", username="a"),
            ChannelDTO(id=2, title="B", username="b"),
        ]

    monkeypatch.setattr(app.channels, "list_monitored_channels", fake_list)
    monitor.set_whitelist.side_effect = lambda ids: calls.append(("whitelist", list(ids)))

    async def fake_monitor_start():
        calls.append("monitor.start")

    monitor.start.side_effect = fake_monitor_start

    async def fake_bootstrap():
        calls.append("bootstrap")
        return ("ready", None)

    monkeypatch.setattr(app, "bootstrap", fake_bootstrap)
    steps: list[str] = []

    result = await app.start(StartOptions(on_step=steps.append))

    assert calls == ["list_monitored", ("whitelist", [1, 2]), "monitor.start", "bootstrap"]
    assert steps == ["whitelist", "monitor", "bootstrap"]
    assert result == StartResult("ready", None, 2, True)


async def test_start_with_explicit_whitelist_skips_storage(monkeypatch) -> None:
    """CLI sync 传显式 channel_ids — 不读 storage 白名单。"""
    app, monitor = _make_app()
    assert monitor is not None

    async def fail_list():
        raise AssertionError("显式白名单时不应读 storage")

    monkeypatch.setattr(app.channels, "list_monitored_channels", fail_list)

    result = await app.start(StartOptions(whitelist=[7, 8], respect_paused=False))

    monitor.set_whitelist.assert_called_once_with([7, 8])
    monitor.start.assert_awaited_once()
    assert result.whitelist_count == 2


async def test_start_without_monitor_skips_realtime(monkeypatch) -> None:
    """start_monitor=False — 只做白名单 + bootstrap(CLI sync 策略)。"""
    app, monitor = _make_app()
    assert monitor is not None

    async def fake_bootstrap():
        return ("ready", None)

    monkeypatch.setattr(app, "bootstrap", fake_bootstrap)
    steps: list[str] = []

    result = await app.start(StartOptions(whitelist=[3], start_monitor=False, on_step=steps.append))

    assert steps == ["whitelist", "bootstrap"]
    monitor.start.assert_not_awaited()
    assert result.monitor_started is False
    assert result.state == "ready"


async def test_start_respects_paused_before_monitor_and_bootstrap(monkeypatch) -> None:
    """paused=True + respect_paused=True — client 保持 uninit,不连 Telegram。"""
    app, monitor = _make_app(paused=True)
    assert monitor is not None

    async def fail_bootstrap():
        raise AssertionError("paused 时不应调 bootstrap")

    monkeypatch.setattr(app, "bootstrap", fail_bootstrap)
    steps: list[str] = []

    result = await app.start(StartOptions(whitelist=[], on_step=steps.append))

    assert steps == ["whitelist", "paused"]
    monitor.start.assert_not_awaited()
    monitor.set_whitelist.assert_called_once_with([])
    assert result.state == "paused"
    assert result.detail is not None and "paused" in result.detail
    assert result.monitor_started is False
    assert result.whitelist_count == 0


async def test_start_ignore_paused_when_disabled(monkeypatch) -> None:
    """--no-resume:respect_paused=False 覆盖 paused,照常 bootstrap。"""
    app, monitor = _make_app(paused=True)

    async def fake_bootstrap():
        return ("ready", None)

    monkeypatch.setattr(app, "bootstrap", fake_bootstrap)

    result = await app.start(StartOptions(whitelist=[], respect_paused=False))

    assert result.state == "ready"
    assert result.monitor_started is True
    assert monitor is not None and monitor.start.await_count == 1


async def test_start_propagates_bootstrap_state(monkeypatch) -> None:
    """bootstrap 非 ready 终态原样透传,monitor_started 如实报告(调用方据此回滚)。"""
    app, _ = _make_app()

    async def fake_bootstrap():
        return ("phone_required", "no session")

    monkeypatch.setattr(app, "bootstrap", fake_bootstrap)

    result = await app.start(StartOptions(whitelist=[]))

    assert result.state == "phone_required"
    assert result.detail == "no session"
    assert result.monitor_started is True


async def test_start_without_monitor_instance_is_safe(monkeypatch) -> None:
    """monitor 未注入时跳过白名单写入与实时流,不崩(单测 / 轻装配场景)。"""
    app, monitor = _make_app(with_monitor=False)
    assert monitor is None

    async def fake_bootstrap():
        return ("ready", None)

    monkeypatch.setattr(app, "bootstrap", fake_bootstrap)

    result = await app.start(StartOptions(whitelist=[4]))

    assert result == StartResult("ready", None, 1, False)
