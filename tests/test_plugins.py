"""3.0.0 插件系统阶段 1 测试 — 发现 / 门禁 / 生命周期 / 隔离 / 配置。

全部离线:本地插件用 tmp_path 造 plugin.toml + plugin.py,entry-point 通道
在测试里被 monkeypatch 成空(装了包的环境不应让测试依赖真实 entry-points)。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tgmonitor.core.config import Settings
from tgmonitor.core.events import EventBus, NotificationRequested
from tgmonitor.plugins.api import EventBusView
from tgmonitor.plugins.config import (
    PluginConfig,
    read_all_config,
    read_enabled,
    write_all_config,
    write_enabled,
)
from tgmonitor.plugins.host import PLUGIN_API_VERSION, PluginHost, PluginState
from tgmonitor.plugins.loader import DiscoveredPlugin, discover_all, discover_local
from tgmonitor.plugins.manifest import PluginManifest, parse_version


def _make_plugin_dir(root: Path, plugin_id: str, *, body: str = "", version: str = "3.0.0") -> Path:
    """造一个本地插件目录:plugin.toml + plugin.py(默认实现一个空 Plugin 类)。"""
    d = root / plugin_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.toml").write_text(
        f'id = "{plugin_id}"\nname = "{plugin_id}"\nmin_app_version = "{version}"\n'
        'capabilities = ["events"]\n',
        encoding="utf-8",
    )
    code = body or (
        "class MyPlugin:\n"
        "    manifest = None\n"
        "    def activate(self, ctx): pass\n"
        "    def deactivate(self): pass\n"
    )
    (d / "plugin.py").write_text(code, encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def _no_entry_points(monkeypatch):
    """测试不依赖真实安装的 entry-points;双通道行为用单测分别覆盖。"""
    monkeypatch.setattr("tgmonitor.plugins.loader.discover_entry_points", lambda: [])


@pytest.fixture
def services():
    return {
        "messages": AsyncMock(),
        "media": AsyncMock(),
        "channels": AsyncMock(),
    }


@pytest.fixture
def settings():
    return Settings.for_test()


# ---------- manifest ----------


def test_parse_version_prefix_and_legacy():
    assert parse_version("3.0.0") == (3, 0, 0)
    assert parse_version("3.1") == (3, 1, 0)
    assert parse_version("v3.2.1rc1") == (3, 2, 1)
    assert parse_version("garbage") == (0, 0, 0)


def test_manifest_id_safety():
    with pytest.raises(ValueError):
        PluginManifest(id="bad id!", name="x")
    with pytest.raises(ValueError):
        PluginManifest(id="", name="x")


def test_manifest_version_gate():
    m = PluginManifest(id="x", name="x", min_app_version="3.1.0")
    assert not m.is_compatible("3.0.0")
    assert m.is_compatible("3.1.0")
    assert m.is_compatible("4.0.0")


def test_manifest_unknown_capabilities():
    m = PluginManifest(id="x", name="x", capabilities=("events", "quantum"))
    assert m.unknown_capabilities() == ("quantum",)


# ---------- config ----------


def test_enabled_roundtrip_and_atomic(tmp_path):
    assert read_enabled(tmp_path) == frozenset()
    write_enabled(tmp_path, frozenset({"b", "a"}))
    assert read_enabled(tmp_path) == frozenset({"a", "b"})
    # Windows 无 POSIX 权限位语义(chmod 只切只读位,stat 恒报 0o666)— 与
    # test_settings_store.py 的 posix_only 同因,权限断言只在 POSIX 有效。
    if sys.platform != "win32":
        assert (tmp_path / "enabled.json").stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "enabled.json.part").exists()


def test_config_corrupt_falls_back_empty(tmp_path):
    (tmp_path / "config.json").write_text("{ broken", encoding="utf-8")
    assert read_all_config(tmp_path) == {}


def test_config_namespace_isolation(tmp_path):
    a = PluginConfig(tmp_path, "plugin_a")
    b = PluginConfig(tmp_path, "plugin_b")
    a.set("token", "secret-a")
    b.set("token", "secret-b")
    assert a.get("token") == "secret-a"
    assert b.get("token") == "secret-b"
    all_data = read_all_config(tmp_path)
    assert all_data["plugin_a"]["token"] == "secret-a"
    assert all_data["plugin_b"]["token"] == "secret-b"
    if sys.platform != "win32":
        assert (tmp_path / "config.json").stat().st_mode & 0o777 == 0o600


def test_config_set_preserves_other_keys(tmp_path):
    write_all_config(tmp_path, {"x": {"keep": 1}})
    PluginConfig(tmp_path, "x").set("add", 2)
    assert read_all_config(tmp_path)["x"] == {"keep": 1, "add": 2}


# ---------- EventBusView ----------


async def test_eventbus_view_subscription_accounting():
    bus = EventBus()
    view = EventBusView(bus, owner="x")
    seen = []

    async def on_event(e):
        seen.append(e)

    view.subscribe(NotificationRequested, on_event)
    assert view.subscription_count() == 1
    await bus.publish(NotificationRequested(title="t", body="b"))
    assert len(seen) == 1

    view.unsubscribe_all()
    assert view.subscription_count() == 0
    await bus.publish(NotificationRequested(title="t2", body="b2"))
    assert len(seen) == 1  # 退订后不再收到
    view.unsubscribe_all()  # 幂等


async def test_eventbus_view_notify_routes_to_notification():
    bus = EventBus()
    seen = []

    async def on_notify(e):
        seen.append(e)

    bus.subscribe(NotificationRequested, on_notify)
    view = EventBusView(bus)
    view.notify("warning", "标题", "正文")
    await bus.flush()
    assert len(seen) == 1
    assert seen[0].title == "标题"
    assert seen[0].body == "正文"


# ---------- loader ----------


def test_local_discovery(tmp_path):
    _make_plugin_dir(tmp_path, "good")
    (tmp_path / "no_manifest").mkdir()
    found = discover_local(tmp_path)
    assert [d.manifest.id for d in found] == ["good"]
    assert found[0].source == "local"


def test_local_discovery_bad_toml_isolated(tmp_path):
    good = _make_plugin_dir(tmp_path, "good")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "plugin.toml").write_text("not valid toml = = =", encoding="utf-8")
    (bad / "plugin.py").write_text("class P: pass", encoding="utf-8")
    found = discover_local(tmp_path)
    assert [d.manifest.id for d in found] == ["good"]
    assert good.exists()


def test_discover_all_local_id_wins(monkeypatch, tmp_path):
    _make_plugin_dir(tmp_path, "dup")

    ep_version = DiscoveredPlugin(
        manifest=PluginManifest(id="dup", name="ep", min_app_version="2.0.0"),
        factory=object,  # type: ignore[arg-type]
        source="entry-point",
    )
    monkeypatch.setattr("tgmonitor.plugins.loader.discover_entry_points", lambda: [ep_version])
    found = discover_all(tmp_path)
    assert len([d for d in found if d.manifest.id == "dup"]) == 1
    assert found[-1].source == "local"


# ---------- host ----------


def test_discovery_is_not_enablement(tmp_path, services, settings):
    _make_plugin_dir(tmp_path, "sleeping")
    host = PluginHost(tmp_path)
    activated = host.activate_all(EventBus(), app_services=services, settings=settings)
    assert activated == []
    rec = host.records()["sleeping"]
    assert rec.state is PluginState.DISABLED
    assert rec.plugin is None


def test_activate_and_deactivate(tmp_path, services, settings):
    marker = tmp_path / "marker.txt"
    body = (
        "from pathlib import Path\n"
        f"MARKER = Path({str(marker)!r})\n"
        "class P:\n"
        "    manifest = None\n"
        "    def activate(self, ctx):\n"
        "        MARKER.write_text('activated')\n"
        "        async def h(e): pass\n"
        "        ctx.bus.subscribe(__import__('tgmonitor.core.events', fromlist=['Event']).Event, h)\n"
        "    def deactivate(self):\n"
        "        MARKER.write_text(MARKER.read_text() + '|deactivated')\n"
    )
    _make_plugin_dir(tmp_path, "active", body=body)
    write_enabled(tmp_path, frozenset({"active"}))
    host = PluginHost(tmp_path)
    bus = EventBus()
    activated = host.activate_all(bus, app_services=services, settings=settings)

    assert len(activated) == 1
    rec = host.records()["active"]
    assert rec.state is PluginState.ACTIVE
    assert marker.read_text() == "activated"
    assert rec.context is not None and rec.context.bus.subscription_count() == 1

    host.deactivate_all()
    assert marker.read_text() == "activated|deactivated"
    assert rec.context is not None and rec.context.bus.subscription_count() == 0
    host.deactivate_all()  # 幂等


def test_version_gate_isolated(tmp_path, services, settings):
    _make_plugin_dir(tmp_path, "too_new", version="9.9.9")
    write_enabled(tmp_path, frozenset({"too_new"}))
    host = PluginHost(tmp_path)
    host.activate_all(EventBus(), app_services=services, settings=settings)
    rec = host.records()["too_new"]
    assert rec.state is PluginState.FAILED
    assert "9.9.9" in (rec.error or "")
    assert PLUGIN_API_VERSION in (rec.error or "")


def test_activate_failure_isolated(tmp_path, services, settings):
    body = (
        "class P:\n"
        "    manifest = None\n"
        "    def activate(self, ctx): raise RuntimeError('boom')\n"
        "    def deactivate(self): pass\n"
    )
    _make_plugin_dir(tmp_path, "exploding", body=body)
    write_enabled(tmp_path, frozenset({"exploding"}))
    host = PluginHost(tmp_path)
    # 坏插件不应上抛,且另一插件不受影响
    activated = host.activate_all(EventBus(), app_services=services, settings=settings)
    assert activated == []
    rec = host.records()["exploding"]
    assert rec.state is PluginState.FAILED
    assert "boom" in (rec.error or "")


def test_bad_module_does_not_block_other_plugin(tmp_path, services, settings):
    _make_plugin_dir(tmp_path, "good")
    _make_plugin_dir(tmp_path, "broken", body="this is not python !!!")
    write_enabled(tmp_path, frozenset({"good", "broken"}))
    host = PluginHost(tmp_path)
    activated = host.activate_all(EventBus(), app_services=services, settings=settings)
    assert [r.manifest.id for r in activated] == ["good"]


# ---------- runtime start/stop 钩子 ----------


def test_start_stop_hooks_lifecycle(tmp_path, services, settings):
    """可选 start/stop 钩子:activate → start → stop → deactivate 全链 + 幂等。"""
    marker = tmp_path / "hooks.txt"
    body = (
        "from pathlib import Path\n"
        f"MARKER = Path({str(marker)!r})\n"
        "class P:\n"
        "    manifest = None\n"
        "    def activate(self, ctx): MARKER.write_text('activated')\n"
        "    def start(self): MARKER.write_text(MARKER.read_text() + '|started')\n"
        "    def stop(self): MARKER.write_text(MARKER.read_text() + '|stopped')\n"
        "    def deactivate(self): MARKER.write_text(MARKER.read_text() + '|deactivated')\n"
    )
    _make_plugin_dir(tmp_path, "hooks", body=body)
    write_enabled(tmp_path, frozenset({"hooks"}))
    host = PluginHost(tmp_path)
    host.activate_all(EventBus(), app_services=services, settings=settings)

    # 未 start 前 stop 是 no-op(幂等守卫)
    host.stop_all()
    host.start_all()
    host.start_all()  # 重复 start 幂等
    assert marker.read_text() == "activated|started"
    host.stop_all()
    host.stop_all()  # 重复 stop 幂等
    assert marker.read_text() == "activated|started|stopped"
    host.deactivate_all()
    assert marker.read_text() == "activated|started|stopped|deactivated"
    # deactivate 后 start 不再生效(插件已非 ACTIVE)
    host.start_all()
    assert marker.read_text() == "activated|started|stopped|deactivated"


def test_start_stop_hooks_are_optional(tmp_path, services, settings):
    """阶段 1/2 老插件(只实现 activate/deactivate)仍可激活,start/stop no-op。"""
    _make_plugin_dir(tmp_path, "good")  # 默认类无 start/stop
    write_enabled(tmp_path, frozenset({"good"}))
    host = PluginHost(tmp_path)
    activated = host.activate_all(EventBus(), app_services=services, settings=settings)
    assert len(activated) == 1
    host.start_all()  # 不抛
    host.stop_all()  # 不抛


def test_start_failure_isolated_and_state_unchanged(tmp_path, services, settings):
    """单个插件 start 抛异常只记日志,不影响其它插件,也不改其 ACTIVE 态。"""
    marker = tmp_path / "hooks.txt"
    bad = (
        "from pathlib import Path\n"
        f"MARKER = Path({str(marker)!r})\n"
        "class P:\n"
        "    manifest = None\n"
        "    def activate(self, ctx): MARKER.write_text('bad:activated;')\n"
        "    def start(self): raise RuntimeError('start boom')\n"
        "    def stop(self): MARKER.write_text(MARKER.read_text() + 'bad:stopped;')\n"
        "    def deactivate(self): pass\n"
    )
    good = (
        "from pathlib import Path\n"
        f"MARKER = Path({str(marker)!r})\n"
        "class P:\n"
        "    manifest = None\n"
        "    def activate(self, ctx):\n"
        "        with MARKER.open('a') as f: f.write('good:activated;')\n"
        "    def start(self):\n"
        "        with MARKER.open('a') as f: f.write('good:started;')\n"
        "    def stop(self):\n"
        "        with MARKER.open('a') as f: f.write('good:stopped;')\n"
        "    def deactivate(self): pass\n"
    )
    _make_plugin_dir(tmp_path, "bad", body=bad)
    _make_plugin_dir(tmp_path, "good", body=good)
    write_enabled(tmp_path, frozenset({"bad", "good"}))
    host = PluginHost(tmp_path)
    host.activate_all(EventBus(), app_services=services, settings=settings)

    host.start_all()  # bad 抛错被吞,good 照常 start
    content = marker.read_text()
    assert "good:started" in content
    assert host.records()["bad"].state is PluginState.ACTIVE  # 不改状态
    host.stop_all()  # 两者 stop 都调用(bad 的 stop 正常)
    content = marker.read_text()
    assert "good:stopped" in content and "bad:stopped" in content


def test_deactivate_all_stops_running_hooks_first(tmp_path, services, settings):
    """即使没显式 stop_all(),deactivate_all() 内部也会先停运行期钩子。"""
    marker = tmp_path / "hooks.txt"
    body = (
        "from pathlib import Path\n"
        f"MARKER = Path({str(marker)!r})\n"
        "class P:\n"
        "    manifest = None\n"
        "    def activate(self, ctx): pass\n"
        "    def start(self): pass\n"
        "    def stop(self): MARKER.write_text('stopped')\n"
        "    def deactivate(self): pass\n"
    )
    _make_plugin_dir(tmp_path, "x", body=body)
    write_enabled(tmp_path, frozenset({"x"}))
    host = PluginHost(tmp_path)
    host.activate_all(EventBus(), app_services=services, settings=settings)
    host.start_all()
    host.deactivate_all()  # 未显式 stop
    assert marker.read_text() == "stopped"


# ---------- API delegation ----------


async def test_api_delegation_to_domain_services(tmp_path, services, settings):
    from tgmonitor.plugins.api import _DelegatingPluginApi

    services["messages"].list_messages.return_value = []
    services["media"].list_media.return_value = ([], 0)
    services["channels"].list_monitored_channels.return_value = []
    api = _DelegatingPluginApi(services)

    await api.list_messages([1], search="x")
    services["messages"].list_messages.assert_awaited_once_with(
        [1], date_from=None, date_to=None, limit=200, search="x"
    )
    await api.set_tags(1, 2, ["a"])
    services["messages"].set_tags.assert_awaited_once_with(1, 2, ["a"])
    await api.retry_media(1, 2, 0)
    services["media"].retry_media.assert_awaited_once_with(1, 2, 0)


# ---------- runtime integration (surface unchanged) ----------


def test_runtime_bootstrap_signature_unchanged():
    import inspect

    from tgmonitor.core import runtime

    sig = inspect.signature(runtime.bootstrap)
    assert list(sig.parameters) == ["settings", "env_path"]
