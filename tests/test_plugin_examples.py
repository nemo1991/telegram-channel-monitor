"""示例插件端到端验证 — 让 `examples/` 成为会被 CI 执行的活文档。

不是照抄示例里的逻辑再断言一遍，而是把示例文件复制到临时 plugins_dir，
用真实的 `discover_local` + `PluginHost` 加载、激活、跑生命周期、拆除。
示例与宿主契约一旦漂移（字段改名 / 顶层 import Qt / exporter 契约变化），
这里就会红。

CLI provider 示例同样真实解析并执行命令；零 Qt import 用子进程验证
（主进程里 PySide6 大概率已被其它测试 import，检查 sys.modules 无意义）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tgmonitor.cli._common import CliError
from tgmonitor.cli.main import build_parser
from tgmonitor.core.config import Settings
from tgmonitor.core.events import EventBus, MessageReceived, NotificationRequested
from tgmonitor.core.export.base import ExporterRegistry
from tgmonitor.plugins.cli import CliCommandProvider
from tgmonitor.plugins.config import write_enabled
from tgmonitor.plugins.host import PluginHost, PluginState

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PLUGIN = PROJECT_ROOT / "examples" / "plugins" / "demo_plugin"
EXAMPLE_CLI = PROJECT_ROOT / "examples" / "cli_provider"


@pytest.fixture(autouse=True)
def _no_entry_points(monkeypatch):
    """示例验证只走本地通道；不让开发环境真实安装的 entry-points 干扰。"""
    monkeypatch.setattr("tgmonitor.plugins.loader.discover_entry_points", lambda: [])


@pytest.fixture
def plugins_dir(tmp_path):
    """复制示例到临时目录（不污染仓库里的示例），并写入启用清单。"""
    dst = tmp_path / "plugins"
    shutil.copytree(EXAMPLE_PLUGIN, dst / "demo_plugin")
    write_enabled(dst, frozenset({"demo_plugin"}))
    return dst


@pytest.fixture
def services():
    return {
        "messages": AsyncMock(),
        "media": AsyncMock(),
        "channels": AsyncMock(),
    }


@pytest.fixture
def activated(plugins_dir, services, monkeypatch):
    """用独立注册表避免污染全局 EXPORTERS，返回 (host, record, registry, bus)。"""
    registry = ExporterRegistry()
    monkeypatch.setattr("tgmonitor.plugins.host.EXPORTERS", registry)
    bus = EventBus()
    host = PluginHost(plugins_dir)
    records = host.activate_all(bus, app_services=services, settings=Settings.for_test())
    assert len(records) == 1
    return host, records[0], registry, bus


# ---------- 本地目录示例：发现 / 激活 / 能力 ----------


def test_example_manifest_files_are_self_consistent():
    """plugin.toml 与 plugin.py 的 manifest 必须一致 —— 通道切换不漂移。"""
    import tomllib

    from tgmonitor.plugins.manifest import PluginManifest

    manifest = PluginManifest.from_dict(
        tomllib.loads((EXAMPLE_PLUGIN / "plugin.toml").read_text(encoding="utf-8"))
    )
    spec = importlib.util.spec_from_file_location(
        "_demo_manifest_check", EXAMPLE_PLUGIN / "plugin.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.DemoPlugin.manifest == manifest


def test_example_discovers_and_activates(activated):
    host, rec, registry, _bus = activated
    assert rec.state is PluginState.ACTIVE
    assert rec.source == "local"
    assert rec.manifest.id == "demo_plugin"
    assert rec.manifest.capabilities == ("events", "export", "ui")
    # export 能力由宿主在 activate 成功后注册
    assert registry.available() == ["demo-json"]
    assert registry.extension("demo-json") == ".demo.json"
    # events 能力：订阅记账在上下文里
    assert rec.context is not None
    assert rec.context.bus.subscription_count() == 1
    # 窄门面：config 是按 id 隔离的命名空间，activate 写了默认值
    assert host.config_for("demo_plugin")["greeting"] == "hello from demo_plugin"


async def test_example_event_subscription_counts_messages(activated):
    _host, rec, _registry, bus = activated
    plugin = rec.plugin
    assert plugin is not None
    bus.publish_async(MessageReceived())
    bus.publish_async(MessageReceived())
    await bus.flush()
    assert plugin.message_count == 2


async def test_example_runtime_hooks_are_live_and_idempotent(activated):
    _host, rec, _registry, bus = activated
    plugin = rec.plugin
    assert plugin is not None

    notifications: list[NotificationRequested] = []

    async def on_notify(event: NotificationRequested) -> None:
        notifications.append(event)

    bus.subscribe(NotificationRequested, on_notify)

    _host.start_all()
    assert plugin._running is True
    await bus.flush()
    assert len(notifications) == 1
    assert notifications[0].title == "示例插件"

    # pause/resume 会反复触发，示例必须幂等
    _host.start_all()
    await bus.flush()
    assert len(notifications) == 1

    _host.stop_all()
    assert plugin._running is False
    _host.stop_all()
    assert plugin._running is False


def test_example_deactivate_removes_all_capabilities(activated, qapp):
    host, rec, registry, _bus = activated
    context = rec.context
    assert context is not None
    page_factory = rec.ui_contribution.page_factory if rec.ui_contribution else None
    assert page_factory is not None

    # UI factory 只在 GUI 装配期调用，返回真实 QWidget
    from PySide6.QtWidgets import QWidget

    assert isinstance(page_factory(), QWidget)

    host.deactivate_all()
    assert registry.available() == []
    assert context.bus.subscription_count() == 0
    assert rec.plugin is not None and rec.plugin._running is False


# ---------- CLI provider 示例：真实解析与执行 ----------


def _load_cli_provider():
    spec = importlib.util.spec_from_file_location("demo_cli_example", EXAMPLE_CLI / "demo_cli.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DemoHelloCommand()


def test_example_cli_provider_satisfies_contract():
    provider = _load_cli_provider()
    assert isinstance(provider, CliCommandProvider)
    assert provider.name == "demo-hello"
    parser = build_parser(cli_providers=[provider])
    args = parser.parse_args(["demo-hello", "--name", "plugin"])
    assert args.cmd == "demo-hello"
    assert args.name == "plugin"


def test_example_cli_provider_runs(capsys):
    provider = _load_cli_provider()
    args = SimpleNamespace(name="plugin")
    assert asyncio.run(provider.run(args)) == 0
    assert "hello, plugin!" in capsys.readouterr().out


def test_example_cli_provider_business_error():
    provider = _load_cli_provider()
    with pytest.raises(CliError, match="不能为空"):
        asyncio.run(provider.run(SimpleNamespace(name="")))


# ---------- 零 Qt 依赖：子进程验证顶层 import ----------


def test_example_plugin_top_level_imports_no_qt():
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(EXAMPLE_PLUGIN)!r}); "
        "import plugin; "
        "assert 'PySide6' not in sys.modules, 'plugin.py 顶层 import 了 Qt'; "
        "assert hasattr(plugin, 'DemoPlugin')"
    )
    env = os.environ.copy()
    src = str(PROJECT_ROOT / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
