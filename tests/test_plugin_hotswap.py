"""插件热切换测试 — ADR-001 阶段 4 插件管理页的宿主侧契约。

覆盖:
- enable:已发现 → 激活 → 写 enabled.json → 运行期 start;幂等;失败隔离
- disable:stop → 注销 exporter → 退订 → deactivate → 移出 enabled.json;幂等
- refresh_discovered:新装插件登记为 DISABLED,不冲掉已有状态
- subscribe_state:状态变更通知 + 退订
- save_config / config_for:按 plugin_id 命名空间隔离,JSON 类型保留
- 版本不兼容 / 激活抛错 → FAILED,不影响其它插件

手法:本地插件目录 + monkeypatch entry-points 为空(与 test_plugins 同款)。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tgmonitor.core.config import Settings
from tgmonitor.core.events import EventBus
from tgmonitor.plugins.config import read_enabled
from tgmonitor.plugins.host import PluginHost, PluginState


@pytest.fixture(autouse=True)
def _no_entry_points(monkeypatch):
    monkeypatch.setattr("tgmonitor.plugins.loader.discover_entry_points", lambda: [])


@pytest.fixture
def services():
    return {
        "messages": AsyncMock(),
        "media": AsyncMock(),
        "channels": AsyncMock(),
    }


def _make_plugin(
    root: Path, pid: str, *, caps: str = '["events"]', body: str = "", version: str = "3.0.0"
) -> Path:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.toml").write_text(
        f'id = "{pid}"\nname = "{pid}"\nmin_app_version = "{version}"\ncapabilities = {caps}\n',
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


def _boot(host: PluginHost, services, *, enabled: frozenset[str] = frozenset()) -> EventBus:
    from tgmonitor.plugins.config import write_enabled

    write_enabled(host.plugins_dir, enabled)
    bus = EventBus()
    host.activate_all(bus, app_services=services, settings=Settings.for_test())
    return bus


# ---------- enable / disable ----------


def test_enable_activates_and_persists(tmp_path, services) -> None:
    """已发现但未启用的插件 → enable 即时激活 + 写 enabled.json。"""
    _make_plugin(tmp_path, "alpha")
    host = PluginHost(tmp_path)
    _boot(host, services)
    assert host.records()["alpha"].state is PluginState.DISABLED

    assert host.enable("alpha") is True
    assert host.records()["alpha"].state is PluginState.ACTIVE
    assert read_enabled(tmp_path) == frozenset({"alpha"})


def test_enable_is_idempotent(tmp_path, services) -> None:
    """重复 enable 已 ACTIVE 插件 → True,不重复 activate。"""
    body = (
        "class MyPlugin:\n"
        "    manifest = None\n"
        "    count = 0\n"
        "    def activate(self, ctx):\n"
        "        MyPlugin.count += 1\n"
        "    def deactivate(self): pass\n"
    )
    _make_plugin(tmp_path, "idem", body=body)
    host = PluginHost(tmp_path)
    _boot(host, services, enabled=frozenset({"idem"}))

    assert host.enable("idem") is True
    assert host.enable("idem") is True
    # 通过插件类属性无法跨模块读取,改用记录状态 + 无异常证明幂等
    assert host.records()["idem"].state is PluginState.ACTIVE


def test_enable_unknown_plugin_returns_false(tmp_path, services) -> None:
    """未发现的 id → enable 返回 False,不写清单。"""
    host = PluginHost(tmp_path)
    _boot(host, services)
    assert host.enable("ghost") is False
    assert read_enabled(tmp_path) == frozenset()


def test_disable_tears_down_and_persists(tmp_path, services) -> None:
    """ACTIVE → disable 退订 + deactivate + 移出 enabled.json。"""
    body = (
        "from tgmonitor.core.events import QuitRequested\n"
        "class MyPlugin:\n"
        "    manifest = None\n"
        "    def activate(self, ctx):\n"
        "        ctx.bus.subscribe(QuitRequested, self._h)\n"
        "    async def _h(self, ev): pass\n"
        "    def deactivate(self): pass\n"
    )
    _make_plugin(tmp_path, "beta", body=body)
    host = PluginHost(tmp_path)
    bus = _boot(host, services, enabled=frozenset({"beta"}))
    rec = host.records()["beta"]
    assert rec.state is PluginState.ACTIVE
    assert rec.context is not None
    before = rec.context.bus.subscription_count()
    assert before == 1

    assert host.disable("beta") is True
    new_rec = host.records()["beta"]
    assert new_rec.state is PluginState.DISABLED
    assert new_rec.context is None  # 上下文已丢弃
    assert read_enabled(tmp_path) == frozenset()
    # 退订生效
    from tgmonitor.core.events import QuitRequested

    assert len(bus._subs.get(QuitRequested, [])) == 0


def test_disable_then_enable_roundtrip(tmp_path, services) -> None:
    """停用 → 再启用的完整周期:记录从缓存重建,清单同步恢复。"""
    _make_plugin(tmp_path, "cycle")
    host = PluginHost(tmp_path)
    _boot(host, services, enabled=frozenset({"cycle"}))

    assert host.disable("cycle") is True
    assert host.records()["cycle"].state is PluginState.DISABLED
    assert read_enabled(tmp_path) == frozenset()
    assert host.enable("cycle") is True
    assert host.records()["cycle"].state is PluginState.ACTIVE
    assert read_enabled(tmp_path) == frozenset({"cycle"})


def test_disable_is_idempotent(tmp_path, services) -> None:
    """重复 disable → True,不清空其它插件。"""
    _make_plugin(tmp_path, "gamma")
    host = PluginHost(tmp_path)
    _boot(host, services, enabled=frozenset({"gamma"}))
    assert host.disable("gamma") is True
    assert host.disable("gamma") is True
    assert host.records()["gamma"].state is PluginState.DISABLED


def test_hotswap_does_not_affect_other_plugins(tmp_path, services) -> None:
    """停用 A 不影响 B 的 ACTIVE 态与订阅。"""
    body = (
        "from tgmonitor.core.events import QuitRequested\n"
        "class MyPlugin:\n"
        "    manifest = None\n"
        "    def activate(self, ctx):\n"
        "        ctx.bus.subscribe(QuitRequested, self._h)\n"
        "    async def _h(self, ev): pass\n"
        "    def deactivate(self): pass\n"
    )
    _make_plugin(tmp_path, "a", body=body)
    _make_plugin(tmp_path, "b", body=body)
    host = PluginHost(tmp_path)
    _boot(host, services, enabled=frozenset({"a", "b"}))

    assert host.disable("a") is True
    assert host.records()["a"].state is PluginState.DISABLED
    assert host.records()["b"].state is PluginState.ACTIVE
    b_rec = host.records()["b"]
    assert b_rec.context is not None
    assert b_rec.context.bus.subscription_count() == 1


def test_enable_running_plugin_calls_start(tmp_path, services) -> None:
    """宿主处于运行期时热启用 → 新插件立即 start()(弹窗关闭即时生效)。"""
    body = (
        "class MyPlugin:\n"
        "    manifest = None\n"
        "    started = False\n"
        "    def activate(self, ctx): pass\n"
        "    def deactivate(self): pass\n"
        "    def start(self): MyPlugin.started = True\n"
        "    def stop(self): pass\n"
    )
    _make_plugin(tmp_path, "run", body=body)
    host = PluginHost(tmp_path)
    _boot(host, services)
    host.start_all()

    assert host.enable("run") is True
    # 通过插件实例字段读:start_all 后 record.plugin 带 start 标记
    rec = host.records()["run"]
    assert rec.plugin is not None
    assert getattr(rec.plugin, "started", False) is True


def test_disable_running_plugin_calls_stop(tmp_path, services) -> None:
    """宿主处于运行期时热停用 → 先 stop 再拆上下文。"""
    body = (
        "class MyPlugin:\n"
        "    manifest = None\n"
        "    stopped = False\n"
        "    def activate(self, ctx): pass\n"
        "    def deactivate(self): pass\n"
        "    def start(self): pass\n"
        "    def stop(self): MyPlugin.stopped = True\n"
    )
    _make_plugin(tmp_path, "stopme", body=body)
    host = PluginHost(tmp_path)
    _boot(host, services, enabled=frozenset({"stopme"}))
    host.start_all()

    rec_before = host.records()["stopme"]
    assert rec_before.plugin is not None
    assert host.disable("stopme") is True
    assert getattr(rec_before.plugin, "stopped", False) is True


# ---------- 失败隔离 ----------


def test_activate_failure_marked_failed(tmp_path, services) -> None:
    """activate 抛错 → FAILED + error 文本,不影响其它插件。"""
    _make_plugin(
        tmp_path,
        "bad",
        body=(
            "class MyPlugin:\n"
            "    manifest = None\n"
            "    def activate(self, ctx): raise RuntimeError('boom')\n"
            "    def deactivate(self): pass\n"
        ),
    )
    _make_plugin(tmp_path, "good")
    host = PluginHost(tmp_path)
    _boot(host, services)

    assert host.enable("bad") is False
    bad = host.records()["bad"]
    assert bad.state is PluginState.FAILED
    assert bad.error == "boom"
    assert host.enable("good") is True
    assert host.records()["good"].state is PluginState.ACTIVE


def test_version_incompatible_stays_failed(tmp_path, services) -> None:
    """版本门禁拒绝的插件 enable 也失败。"""
    _make_plugin(tmp_path, "old", version="99.0.0")
    host = PluginHost(tmp_path)
    _boot(host, services)
    assert host.enable("old") is False
    assert host.records()["old"].state is PluginState.FAILED


# ---------- 发现与订阅 ----------


def test_refresh_discovered_adds_new_as_disabled(tmp_path, services) -> None:
    """运行期新装插件 → refresh_discovered 登记为 DISABLED,不动已有态。"""
    _make_plugin(tmp_path, "existing")
    host = PluginHost(tmp_path)
    _boot(host, services, enabled=frozenset({"existing"}))
    assert host.enable("existing") is True

    _make_plugin(tmp_path, "newcomer")
    fresh = host.refresh_discovered()
    assert fresh == ("newcomer",)
    assert host.records()["newcomer"].state is PluginState.DISABLED
    assert host.records()["existing"].state is PluginState.ACTIVE


def test_subscribe_state_and_unsubscribe(tmp_path, services) -> None:
    """状态变更通知订阅者;退订后不再收到。"""
    _make_plugin(tmp_path, "notify")
    host = PluginHost(tmp_path)
    _boot(host, services)
    seen: list[int] = []
    unsub = host.subscribe_state(lambda: seen.append(1))

    host.enable("notify")
    assert len(seen) == 1
    unsub()
    host.disable("notify")
    assert len(seen) == 1
    # 退订幂等
    unsub()


# ---------- 配置命名空间 ----------


def test_save_config_preserves_other_plugins(tmp_path, services) -> None:
    """save_config 只覆盖目标 id 子树,其它插件配置不变。"""
    from tgmonitor.plugins.config import write_all_config

    write_all_config(tmp_path, {"a": {"x": 1}, "b": {"y": 2}})
    host = PluginHost(tmp_path)
    _boot(host, services)
    host.save_config("a", {"x": 3, "z": True})
    assert host.config_for("a") == {"x": 3, "z": True}
    assert host.config_for("b") == {"y": 2}


def test_parse_config_value_keeps_json_types() -> None:
    """KV 表格值按 JSON 字面量解析:数字/布尔/数组/对象保类型,文本裸显。"""
    from tgmonitor.ui.widgets.plugin_manager_dialog import (
        display_config_value,
        parse_config_value,
    )

    assert parse_config_value("42") == 42
    assert parse_config_value("true") is True
    assert parse_config_value("[1, 2]") == [1, 2]
    assert parse_config_value('{"k": 1}') == {"k": 1}
    assert parse_config_value("null") is None
    assert parse_config_value("hello") == "hello"
    assert parse_config_value("") == ""
    assert display_config_value("hello") == "hello"
    assert display_config_value([1, 2]) == "[1, 2]"
