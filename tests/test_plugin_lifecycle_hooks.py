"""插件运行期钩子接入 AppService 生命周期的测试 — ADR-001 阶段 3。

四个接入点:
- `app.start()` 成功(ready)→ start_all;paused / error 不触发
- `app.shutdown()` → stop_all(先停插件再关后端)
- `pause_monitor()` 成功路径 → stop_all
- `resume_monitor()` 成功路径 → start_all;失败分支不触发

手法:monkeypatch `tgmonitor.plugins.host.PLUGIN_HOST` 单例为记录调用的 spy。
core 侧都用 lazy import,所以 patch 在方法调用时生效。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tgmonitor.core.app_service import AppService, StartOptions
from tgmonitor.core.app_service._startup import (
    StartResult as StartupResult,
)
from tgmonitor.core.config import Settings
from tgmonitor.core.events import EventBus
from tgmonitor.core.telegram.client import TelegramClient


class _HostSpy:
    """记录 start_all / stop_all 调用次数的宿主替身。"""

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.start_error: Exception | None = None

    def start_all(self) -> None:
        self.starts += 1
        if self.start_error is not None:
            raise self.start_error

    def stop_all(self) -> None:
        self.stops += 1


@pytest.fixture
def host_spy(monkeypatch) -> _HostSpy:
    spy = _HostSpy()
    monkeypatch.setattr("tgmonitor.plugins.host.PLUGIN_HOST", spy)
    return spy


def _make_app(*, paused: bool = False) -> tuple[AppService, MagicMock]:
    bus = EventBus()
    client = AsyncMock(spec=TelegramClient)
    client.start.return_value = ("ready", None)
    monitor = MagicMock()
    monitor.start = AsyncMock()
    monitor.stop = AsyncMock()
    monitor.set_whitelist = MagicMock()
    app = AppService(
        bus=bus,
        client=client,  # type: ignore[arg-type]
        storage=MagicMock(),  # type: ignore[arg-type]
        objects=MagicMock(),  # type: ignore[arg-type]
        settings=Settings.for_test(paused=paused),
        monitor=monitor,  # type: ignore[arg-type]
    )
    return app, monitor


# ---------- app.start ----------


async def test_start_ready_triggers_plugin_start(host_spy: _HostSpy, monkeypatch) -> None:
    """app.start() 成功 → start_all 一次。"""
    app, _ = _make_app()

    async def fake_bootstrap():
        return ("ready", None)

    monkeypatch.setattr(app, "bootstrap", fake_bootstrap)
    await app.start(StartOptions(whitelist=[]))
    assert host_spy.starts == 1
    assert host_spy.stops == 0


async def test_start_paused_does_not_trigger_plugin_start(host_spy: _HostSpy) -> None:
    """paused 短路 → 不 start;插件留到 resume 时启动。"""
    app, _ = _make_app(paused=True)
    result = await app.start(StartOptions(whitelist=[]))
    assert result.state == "paused"
    assert host_spy.starts == 0


async def test_start_error_does_not_trigger_plugin_start(host_spy: _HostSpy, monkeypatch) -> None:
    """bootstrap 非 ready → 会话没连上,不 start。"""
    app, _ = _make_app()

    async def fake_bootstrap():
        return ("phone_required", "no session")

    monkeypatch.setattr(app, "bootstrap", fake_bootstrap)
    result = await app.start(StartOptions(whitelist=[]))
    assert result.state == "phone_required"
    assert host_spy.starts == 0


async def test_start_plugin_failure_does_not_block(host_spy: _HostSpy, monkeypatch) -> None:
    """插件 start_all 抛异常被吞,start() 照常返回结果。"""
    app, _ = _make_app()
    host_spy.start_error = RuntimeError("plugin import boom")

    async def fake_bootstrap():
        return ("ready", None)

    monkeypatch.setattr(app, "bootstrap", fake_bootstrap)
    result = await app.start(StartOptions(whitelist=[]))
    assert result == StartupResult("ready", None, 0, True)


# ---------- pause / resume ----------


async def test_pause_triggers_plugin_stop(host_spy: _HostSpy) -> None:
    app, _ = _make_app()
    await app.pause_monitor()
    assert host_spy.stops == 1
    assert host_spy.starts == 0


async def test_pause_idempotent_no_repeated_stop(host_spy: _HostSpy) -> None:
    app, _ = _make_app()
    await app.pause_monitor()
    await app.pause_monitor()
    assert host_spy.stops == 1


async def test_resume_triggers_plugin_start(host_spy: _HostSpy) -> None:
    app, _ = _make_app()
    await app.pause_monitor()
    host_spy.starts = 0  # 清零,只观测 resume 的贡献
    await app.resume_monitor()
    assert host_spy.starts == 1


async def test_resume_failure_does_not_trigger_plugin_start(host_spy: _HostSpy) -> None:
    """client.start 失败保持 paused → 不 start 插件(与 P2 #35 假状态防线一致)。"""
    app, _ = _make_app()
    app.client.start.side_effect = RuntimeError("client boom")  # type: ignore[union-attr]
    await app.pause_monitor()
    host_spy.starts = 0
    await app.resume_monitor()
    assert app.is_paused is True
    assert host_spy.starts == 0


async def test_pause_resume_cycle_signals_both(host_spy: _HostSpy) -> None:
    """完整周期:每次 pause→stop,每次 resume→start。"""
    app, _ = _make_app()
    await app.pause_monitor()
    await app.resume_monitor()
    await app.pause_monitor()
    await app.resume_monitor()
    assert host_spy.stops == 2
    assert host_spy.starts == 2


# ---------- shutdown ----------


async def test_shutdown_triggers_plugin_stop(host_spy: _HostSpy) -> None:
    """app.shutdown() 开头 → stop_all,先停插件再关后端。"""
    app, _ = _make_app()
    await app.shutdown()
    assert host_spy.stops == 1
