"""插件系统错误场景 — 专攻「失败后是否留下幽灵状态」与「失败是否被隔离」。

与阶段 1~4 的单测不同，这里断言的是失败之后的**系统完整性**：
- 激活失败后事件订阅 / exporter 是否回滚干净；
- 畸形发现输入是否只跳过坏插件；
- 热切换的写盘 / 通知失败是否拖垮主流程；
- 运行期钩子失败是否隔离；
- CLI 插件命令错误路径的退出码。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tgmonitor.cli._common import CliError
from tgmonitor.cli.main import main
from tgmonitor.core.config import Settings
from tgmonitor.core.events import EventBus, MessageReceived
from tgmonitor.core.export.base import Exporter, ExporterRegistry
from tgmonitor.plugins.config import read_enabled
from tgmonitor.plugins.host import PluginHost, PluginState
from tgmonitor.plugins.loader import discover_entry_points as _real_discover_entry_points
from tgmonitor.plugins.loader import discover_local
from tgmonitor.plugins.manifest import PluginManifest


def _make_plugin_dir(
    root: Path,
    plugin_id: str,
    *,
    body: str = "",
    caps: tuple[str, ...] = ("events",),
    version: str = "3.0.0",
    toml_id: str | None = None,
) -> Path:
    d = root / plugin_id
    d.mkdir(parents=True, exist_ok=True)
    caps_str = ", ".join(f'"{c}"' for c in caps)
    (d / "plugin.toml").write_text(
        f'id = "{toml_id if toml_id is not None else plugin_id}"\n'
        f'name = "{plugin_id}"\nversion = "1.0.0"\n'
        f'min_app_version = "{version}"\ncapabilities = [{caps_str}]\n',
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


def _enable(root: Path, *ids: str) -> None:
    """写启用清单 —— 发现 ≠ 启用，激活测试必须显式启用。"""
    (root / "enabled.json").write_text(json.dumps({"enabled": list(ids)}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_entry_points(monkeypatch):
    monkeypatch.setattr("tgmonitor.plugins.loader.discover_entry_points", lambda: [])


@pytest.fixture
def services():
    return {"messages": AsyncMock(), "media": AsyncMock(), "channels": AsyncMock()}


@pytest.fixture
def fresh_registry(monkeypatch):
    registry = ExporterRegistry()
    monkeypatch.setattr("tgmonitor.plugins.host.EXPORTERS", registry)
    return registry


def _activate(host, bus, services):
    return host.activate_all(bus, app_services=services, settings=Settings.for_test())


# ---------- A. 激活失败的副作用回滚 ----------


def test_activate_failure_rolls_back_event_subscriptions(tmp_path, services):
    """activate() 中途订阅后抛错 → 订阅必须回滚，不留幽灵订阅者。"""
    _make_plugin_dir(
        tmp_path,
        "bad",
        body=(
            "from tgmonitor.core.events import MessageReceived\n"
            "class Bad:\n"
            "    manifest = None\n"
            "    def activate(self, ctx):\n"
            "        async def h(ev): pass\n"
            "        ctx.bus.subscribe(MessageReceived, h)\n"
            "        raise RuntimeError('subscribe then boom')\n"
            "    def deactivate(self): pass\n"
        ),
    )
    _enable(tmp_path, "bad")
    bus = EventBus()
    host = PluginHost(tmp_path)
    _activate(host, bus, services)

    rec = host.records()["bad"]
    assert rec.state is PluginState.FAILED
    assert "subscribe then boom" in rec.error
    assert bus._subs.get(MessageReceived, []) == []


def test_ui_getter_failure_marks_failed_without_exporter_residue(
    tmp_path, services, fresh_registry
):
    """UI 贡献 getter 抛错 → FAILED，且此前注册的 exporter 必须回滚。

    `_activate_one` 的顺序是 activate → exporter 注册 → ui 读取。若 ui 阶段抛错，
    已注册的 exporter 没有回滚就是「插件失败但格式还活着」的幽灵状态。
    """
    _make_plugin_dir(
        tmp_path,
        "bad",
        caps=("export", "ui"),
        body=(
            "from tgmonitor.core.export.base import Exporter\n"
            "class X(Exporter):\n"
            "    format = 'ghost-format'\n"
            "    async def render(self, *a, **kw): return 0\n"
            "class Bad:\n"
            "    manifest = None\n"
            "    exporters = (X(),)\n"
            "    def activate(self, ctx): pass\n"
            "    def deactivate(self): pass\n"
            "    @property\n"
            "    def ui_contribution(self): raise RuntimeError('ui boom')\n"
        ),
    )
    write_enabled = tmp_path / "enabled.json"
    write_enabled.write_text(json.dumps({"enabled": ["bad"]}), encoding="utf-8")

    host = PluginHost(tmp_path)
    _activate(host, EventBus(), services)

    rec = host.records()["bad"]
    assert rec.state is PluginState.FAILED
    assert "ui boom" in rec.error
    assert fresh_registry.available() == [], "激活失败后不得留下幽灵导出格式"


def test_exporter_conflict_isolated_between_plugins(tmp_path, services, fresh_registry):
    """插件 A 的 format 与已注册冲突 → A FAILED，插件 B 照常激活。"""

    class BuiltinLike(Exporter):
        format = "json"

        async def render(self, *args, **kwargs) -> int:
            return 0

    fresh_registry.register(BuiltinLike())

    _make_plugin_dir(
        tmp_path,
        "conflict",
        caps=("export",),
        body=(
            "from tgmonitor.core.export.base import Exporter\n"
            "class X(Exporter):\n"
            "    format = 'json'\n"
            "    async def render(self, *a, **kw): return 0\n"
            "class Bad:\n"
            "    manifest = None\n"
            "    exporters = (X(),)\n"
            "    def activate(self, ctx): pass\n"
            "    def deactivate(self): pass\n"
        ),
    )
    _make_plugin_dir(
        tmp_path,
        "good",
        caps=("export",),
        body=(
            "from tgmonitor.core.export.base import Exporter\n"
            "class Y(Exporter):\n"
            "    format = 'good-format'\n"
            "    async def render(self, *a, **kw): return 0\n"
            "class Good:\n"
            "    manifest = None\n"
            "    exporters = (Y(),)\n"
            "    def activate(self, ctx): pass\n"
            "    def deactivate(self): pass\n"
        ),
    )
    (tmp_path / "enabled.json").write_text(
        json.dumps({"enabled": ["conflict", "good"]}), encoding="utf-8"
    )
    host = PluginHost(tmp_path)
    _activate(host, EventBus(), services)

    assert host.records()["conflict"].state is PluginState.FAILED
    assert host.records()["good"].state is PluginState.ACTIVE
    assert fresh_registry.available() == ["json", "good-format"]


# ---------- B. 畸形发现输入 ----------


def test_local_dir_name_wins_over_manifest_id(tmp_path, caplog):
    """目录名是启用清单 / 配置命名空间的 key，必须与 manifest.id 冲突时以目录名为准。"""
    _make_plugin_dir(tmp_path, "renamed", toml_id="original")
    found = discover_local(tmp_path)
    assert len(found) == 1
    assert found[0].manifest.id == "renamed"
    assert "以目录名为准" in caplog.text


def test_local_dir_missing_files_are_skipped(tmp_path):
    """缺 plugin.py / plugin.toml 的目录静默跳过，不影响正常插件。"""
    (tmp_path / "no_code").mkdir()
    (tmp_path / "no_code" / "plugin.toml").write_text('id = "no_code"\n', encoding="utf-8")
    (tmp_path / "no_toml").mkdir()
    (tmp_path / "no_toml" / "plugin.py").write_text("class X: pass\n", encoding="utf-8")
    _make_plugin_dir(tmp_path, "good")
    found = discover_local(tmp_path)
    assert [item.manifest.id for item in found] == ["good"]


def test_entry_point_bad_manifest_type_isolated(monkeypatch):
    """entry-point 指向的类 manifest 类型非法 → 跳过该条，不阻断发现通道。"""

    class _EP:
        def __init__(self, name, obj):
            self.name = name
            self._obj = obj

        def load(self):
            return self._obj

    class Bad:
        manifest = 123  # 既不是 PluginManifest 也不是 dict

        def activate(self, ctx):
            pass

        def deactivate(self):
            pass

    class Good:
        manifest = PluginManifest(id="good", name="good")

        def activate(self, ctx):
            pass

        def deactivate(self):
            pass

    # 真实函数体里 `entry_points` 是局部 import，patch 源模块符号即可。
    # 用模块加载时就保存的真实函数引用，绕开本文件 autouse 的屏蔽。
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda **kw: [_EP("bad", Bad), _EP("good", Good)],
    )
    found = _real_discover_entry_points()
    assert [item.manifest.id for item in found] == ["good"]


def test_module_with_multiple_plugin_classes_picks_deterministically(tmp_path):
    """模块里有多个符合契约的类时，取 dir() 序第一个 —— 结果确定、可预期。"""
    _make_plugin_dir(
        tmp_path,
        "multi",
        body=(
            "class ZPlugin:\n"
            "    manifest = None\n"
            "    def activate(self, ctx): pass\n"
            "    def deactivate(self): pass\n"
            "class APlugin:\n"
            "    manifest = None\n"
            "    def activate(self, ctx): pass\n"
            "    def deactivate(self): pass\n"
        ),
    )
    found = discover_local(tmp_path)
    assert len(found) == 1
    assert found[0].factory.__name__ == "APlugin"


# ---------- C. 热切换的写盘 / 通知失败安全 ----------


def test_enable_survives_enabled_write_failure(tmp_path, services, monkeypatch):
    """enabled.json 写盘失败 → 内存态仍启用、返回 True，不把 IO 错误抛给用户。"""
    _make_plugin_dir(tmp_path, "good")
    _enable(tmp_path, "good")
    host = PluginHost(tmp_path)
    _activate(host, EventBus(), services)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("tgmonitor.plugins.host.write_enabled", boom)
    assert host.enable("good") is True
    assert host.records()["good"].state is PluginState.ACTIVE


def test_state_listener_failure_isolated(tmp_path, services):
    """一个状态订阅者抛错 → 其它订阅者照常收到，enable 主流程不受影响。"""
    _make_plugin_dir(tmp_path, "good")
    _enable(tmp_path, "good")
    host = PluginHost(tmp_path)
    _activate(host, EventBus(), services)
    host.disable("good")

    seen: list[str] = []

    def bad_listener():
        seen.append("bad")
        raise RuntimeError("listener boom")

    host.subscribe_state(bad_listener)
    host.subscribe_state(lambda: seen.append("good"))
    assert host.enable("good") is True
    assert seen == ["bad", "good"]


def test_corrupt_config_recovers_on_save(tmp_path):
    """config.json 结构损坏 → 读回空；save_config 能覆盖恢复。"""
    (tmp_path / "config.json").write_text('"not a dict"', encoding="utf-8")
    host = PluginHost(tmp_path)
    assert host.config_for("x") == {}
    host.save_config("x", {"k": 42})
    assert host.config_for("x") == {"k": 42}


def test_corrupt_enabled_treated_as_empty(tmp_path, services):
    """enabled.json 类型不符（list 而非 dict）→ 视为无插件启用，默认安全。"""
    _make_plugin_dir(tmp_path, "good")
    (tmp_path / "enabled.json").write_text('["good"]', encoding="utf-8")
    assert read_enabled(tmp_path) == frozenset()
    host = PluginHost(tmp_path)
    _activate(host, EventBus(), services)
    assert host.records()["good"].state is PluginState.DISABLED


# ---------- D. 运行期钩子边界 ----------


def test_stop_failure_isolated_across_plugins(tmp_path, services):
    """插件 stop 抛错 → 其它插件 stop 照常调用，stop_all 不抛。"""
    _make_plugin_dir(
        tmp_path,
        "bad",
        body=(
            "class Bad:\n"
            "    manifest = None\n"
            "    def activate(self, ctx): pass\n"
            "    def deactivate(self): pass\n"
            "    def start(self): pass\n"
            "    def stop(self): raise RuntimeError('stop boom')\n"
        ),
    )
    _make_plugin_dir(
        tmp_path,
        "good",
        body=(
            "class Good:\n"
            "    manifest = None\n"
            "    def activate(self, ctx): pass\n"
            "    def deactivate(self): pass\n"
            "    def start(self): self.started = True\n"
            "    def stop(self): self.stopped = True\n"
        ),
    )
    (tmp_path / "enabled.json").write_text(
        json.dumps({"enabled": ["bad", "good"]}), encoding="utf-8"
    )
    host = PluginHost(tmp_path)
    _activate(host, EventBus(), services)
    host.start_all()
    host.stop_all()  # 不抛
    good = host.records()["good"].plugin
    assert getattr(good, "started", False) and getattr(good, "stopped", False)


def test_start_stop_before_activate_is_noop(tmp_path):
    """未 activate 时 start_all / stop_all 都是 no-op（冷启动边缘路径）。"""
    host = PluginHost(tmp_path)
    host.start_all()
    host.stop_all()
    assert host.records() == {}


# ---------- E. CLI 插件命令错误路径 ----------


class _FakeProvider:
    name = "demo"

    def __init__(self, error: Exception):
        self.error = error

    def configure_parser(self, subparsers):
        subparsers.add_parser(self.name)

    async def run(self, args):
        raise self.error


def test_cli_plugin_business_error_maps_to_exit_code_1(monkeypatch, capsys):
    monkeypatch.setattr(
        "tgmonitor.plugins.cli.discover_cli_commands",
        lambda: [_FakeProvider(CliError("bad input"))],
    )
    assert main(["demo"]) == 1
    assert "error: bad input" in capsys.readouterr().err


def test_cli_plugin_unexpected_error_propagates_for_diagnostics(monkeypatch):
    """未预期异常保持冒泡（traceback 完整、退出码非零），便于诊断第三方代码。

    这是有意选择：CLI 每次调用是独立进程，冒泡不阻断其它命令；把它吞成
    「error: ...」会丢失 traceback，反而降低第三方插件的可诊断性。
    """
    monkeypatch.setattr(
        "tgmonitor.plugins.cli.discover_cli_commands",
        lambda: [_FakeProvider(RuntimeError("plugin bug"))],
    )
    with pytest.raises(RuntimeError, match="plugin bug"):
        main(["demo"])
