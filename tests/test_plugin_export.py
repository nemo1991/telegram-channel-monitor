"""3.0.0 插件系统阶段 2 测试 — 导出格式插件化。

覆盖:注册 key 放宽到 str / unregister 对称 / 元数据驱动 object_store 与缩略图 /
host 注册与注销插件 exporter / 失败隔离 / UI 下拉与 CLI 运行时派生。

全部离线;全局 EXPORTERS 在每个测试后恢复到进入时的快照,测试间不串味。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tgmonitor.core.config import Settings
from tgmonitor.core.dto import ExportFormat
from tgmonitor.core.events import EventBus
from tgmonitor.core.export import service as _export_service  # noqa: F401 — 触发内置 @exporter
from tgmonitor.core.export.base import EXPORTERS, Exporter, ExporterRegistry, _format_key
from tgmonitor.plugins.config import write_enabled
from tgmonitor.plugins.host import PluginHost, PluginState


class _PluginExporter(Exporter):
    """测试用插件格式(不在 ExportFormat 枚举里)。"""

    format = "plugin_fmt"
    extension = ".plg"
    needs_object_store = False
    supports_thumbnails = True

    async def render(
        self,
        out_path,
        channels,
        messages,
        *,
        object_store=None,
        include_thumbnails=False,
        include_metadata=True,
        storage=None,
    ) -> int:
        return 0


@pytest.fixture(autouse=True)
def _restore_registry():
    """每个测试后恢复全局注册表,避免插件格式泄漏到其它测试。"""
    snapshot = dict(EXPORTERS._items)
    yield
    EXPORTERS._items.clear()
    EXPORTERS._items.update(snapshot)


@pytest.fixture(autouse=True)
def _no_entry_points(monkeypatch):
    monkeypatch.setattr("tgmonitor.plugins.loader.discover_entry_points", lambda: [])


@pytest.fixture
def services():
    return {"messages": AsyncMock(), "media": AsyncMock(), "channels": AsyncMock()}


@pytest.fixture
def settings():
    return Settings.for_test()


# ---------- 注册表语义 ----------


def test_normalizes_enum_and_plain_str_to_same_key():
    """str-Enum 成员与同名 str 收敛到同一个纯 str key。"""
    assert _format_key(ExportFormat.JSON) == "json"
    assert _format_key("json") == "json"
    assert type(_format_key(ExportFormat.JSON)) is str
    reg = ExporterRegistry()
    assert reg.register(_PluginExporter()) == "plugin_fmt"
    assert reg.get("plugin_fmt") is reg.get("plugin_fmt")


def test_enum_member_key_is_normalized():
    """内置 exporter 传枚举成员,注册表里存的仍是纯 str。"""
    reg = ExporterRegistry()

    class EnumFmt(Exporter):
        format = ExportFormat.JSON

        async def render(self, *a, **kw) -> int:
            return 0

    assert reg.register(EnumFmt()) == "json"
    assert reg.available() == ["json"]
    assert reg.get("json") is reg.get(ExportFormat.JSON)


def test_duplicate_register_rejected_until_unregister():
    reg = ExporterRegistry()
    reg.register(_PluginExporter())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_PluginExporter())
    assert reg.unregister("plugin_fmt") is True
    assert reg.unregister("plugin_fmt") is False  # 幂等
    reg.register(_PluginExporter())  # 注销后可重注册 —— 插件重激活的关键


def test_metadata_drives_object_store_and_thumbnails():
    reg = ExporterRegistry()
    reg.register(_PluginExporter())
    assert reg.needs_object_store("plugin_fmt") is False
    assert reg.supports_thumbnails("plugin_fmt") is True
    assert reg.extension("plugin_fmt") == ".plg"
    with pytest.raises(KeyError):
        reg.get("not_registered")


def test_builtin_formats_stable_and_plugin_appends():
    """插件格式不改变内置格式的顺序与集合。"""
    before = EXPORTERS.message_formats()
    assert before == ["json", "csv", "markdown", "html", "zip"]
    EXPORTERS.register(_PluginExporter())
    after = EXPORTERS.message_formats()
    assert after == [*before, "plugin_fmt"]
    assert "plugin_fmt" not in EXPORTERS.media_formats()


# ---------- host 生命周期 ----------


def _make_export_plugin(
    root: Path, plugin_id: str, *, caps: str = '["export"]', body_extra: str = ""
) -> Path:
    d = root / plugin_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.toml").write_text(
        f'id = "{plugin_id}"\nname = "{plugin_id}"\n'
        f'min_app_version = "3.0.0"\ncapabilities = {caps}\n',
        encoding="utf-8",
    )
    (d / "plugin.py").write_text(
        "class CustomExporter:\n"
        f"    format = '{plugin_id}_fmt'\n"
        f"    extension = '.{plugin_id}'\n"
        "    async def render(self, *a, **kw): return 0\n\n"
        "class MyPlugin:\n"
        "    manifest = None\n"
        "    exporters = (CustomExporter,)\n" + body_extra + "    def activate(self, ctx): pass\n"
        "    def deactivate(self): pass\n",
        encoding="utf-8",
    )
    return d


def test_host_registers_on_activate_and_unregisters_on_deactivate(tmp_path, services, settings):
    pid = "exporter_plugin"
    _make_export_plugin(tmp_path, pid)
    write_enabled(tmp_path, frozenset({pid}))

    host = PluginHost(tmp_path)
    activated = host.activate_all(EventBus(), app_services=services, settings=settings)
    assert len(activated) == 1
    assert activated[0].state is PluginState.ACTIVE
    assert f"{pid}_fmt" in EXPORTERS.available()
    assert activated[0].export_formats == (f"{pid}_fmt",)

    host.deactivate_all()
    assert f"{pid}_fmt" not in EXPORTERS.available()
    host.deactivate_all()  # 幂等
    assert f"{pid}_fmt" not in EXPORTERS.available()


def test_host_reactivation_after_deactivate(tmp_path, services, settings):
    """停用后重新发现激活,不能撞 register 的重名保护。"""
    pid = "replugin"
    _make_export_plugin(tmp_path, pid)
    write_enabled(tmp_path, frozenset({pid}))

    host = PluginHost(tmp_path)
    host.activate_all(EventBus(), app_services=services, settings=settings)
    assert f"{pid}_fmt" in EXPORTERS.available()
    host.deactivate_all()
    host.activate_all(EventBus(), app_services=services, settings=settings)
    assert f"{pid}_fmt" in EXPORTERS.available()
    host.deactivate_all()


def test_export_capability_missing_providers_is_isolated(tmp_path, services, settings):
    """声明 export 但没给 exporters:告警、插件仍激活、无格式残留。"""
    pid = "noprovider"
    _make_export_plugin(tmp_path, pid)
    # 去掉 exporters 行
    p = tmp_path / pid / "plugin.py"
    p.write_text(
        p.read_text(encoding="utf-8").replace("    exporters = (CustomExporter,)\n", ""),
        encoding="utf-8",
    )
    write_enabled(tmp_path, frozenset({pid}))

    host = PluginHost(tmp_path)
    activated = host.activate_all(EventBus(), app_services=services, settings=settings)
    assert len(activated) == 1
    assert activated[0].export_formats == ()
    assert f"{pid}_fmt" not in EXPORTERS.available()
    host.deactivate_all()


def test_duplicate_format_fails_plugin_and_rolls_back(tmp_path, services, settings):
    """插件格式与内置重名:插件 FAILED,已注册的前序格式回滚,不污染全局。"""
    pid = "collision"
    _make_export_plugin(tmp_path, pid)
    p = tmp_path / pid / "plugin.py"
    code = p.read_text(encoding="utf-8").replace(
        "    exporters = (CustomExporter,)\n",
        "    exporters = (CustomExporter, CustomExporter)\n",  # 同插件内第二个撞重名
    )
    p.write_text(code, encoding="utf-8")
    write_enabled(tmp_path, frozenset({pid}))

    before = set(EXPORTERS.available())
    host = PluginHost(tmp_path)
    activated = host.activate_all(EventBus(), app_services=services, settings=settings)
    assert activated == []
    rec = host.records()[pid]
    assert rec.state is PluginState.FAILED
    assert set(EXPORTERS.available()) == before
    host.deactivate_all()


# ---------- UI / CLI 派生 ----------


def test_dialog_shows_plugin_format_at_runtime(qapp):
    """dialog 每次构造从注册表实时派生 —— 插件格式进得了下拉。"""
    EXPORTERS.register(_PluginExporter())
    from tgmonitor.ui.widgets.export_dialog import ExportDialog

    dlg = ExportDialog(None, [1])
    data = [dlg.cmb_fmt.itemData(i) for i in range(dlg.cmb_fmt.count())]
    assert "plugin_fmt" in data
    dlg.cmb_fmt.setCurrentIndex(data.index("plugin_fmt"))
    dlg.in_path.setText("/tmp/out")  # 无后缀 -> 用注册表 extension 补
    dlg._on_ok()
    assert dlg.request().format == "plugin_fmt"
    assert dlg.request().out_path.endswith(".plg")
    dlg.close()


def test_cli_parse_does_not_statically_limit_format():
    """插件在 bootstrap 才激活,build_parser 不能锁死 choices。"""
    from tgmonitor.cli.main import build_parser

    parser = build_parser()
    args = parser.parse_args(["export", "messages", "--format", "plugin_fmt", "-o", "/tmp/out"])
    assert args.format == "plugin_fmt"
