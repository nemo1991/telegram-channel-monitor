"""UI 扩展点测试 — ADR-001 阶段 4:UiContribution 收集 + 导航注册 + 页面注入。

分层:
- host 层(无 Qt):ui_contribution 只在声明 ui 能力时读,类型不符忽略
- nav 层(Qt):add_item 追加项、索引/信号正确
- 装配层(Qt):真实 MainWindow 构造时注入插件页,nav 与 stack 同索引

插件注入测试用 monkeypatch 替换 PLUGIN_HOST 为一个带 records() 的替身,
不依赖真实包安装。
"""

from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtWidgets import QWidget

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.core.config import Settings
from tgmonitor.core.events import EventBus
from tgmonitor.plugins.api import UiContribution
from tgmonitor.plugins.config import write_enabled
from tgmonitor.plugins.host import PluginHost, PluginState


def _make_plugin_dir(root, plugin_id: str, *, body: str = "") -> None:
    """造本地插件目录 — 与 test_plugins 同手法,内联避免跨测试模块依赖。"""
    d = root / plugin_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.toml").write_text(
        f'id = "{plugin_id}"\nname = "{plugin_id}"\nmin_app_version = "3.0.0"\n'
        'capabilities = ["events"]\n',
        encoding="utf-8",
    )
    (d / "plugin.py").write_text(body, encoding="utf-8")


# ---------- host 层:收集(无 Qt) ----------


class _FakeHost:
    """带 records() 的宿主替身 — 供装配层测试注入。"""

    def __init__(self, contributions: dict[str, UiContribution | None]) -> None:
        self._records = {
            pid: SimpleNamespace(ui_contribution=c, state=PluginState.ACTIVE)
            for pid, c in contributions.items()
        }

    def records(self):
        return self._records


def _read_ui(host: PluginHost, pid: str) -> UiContribution | None:
    return host.records()[pid].ui_contribution


def test_ui_contribution_only_for_ui_capability(tmp_path) -> None:
    """声明 ui 能力 + 提供 ui_contribution → 收集到。"""
    body = (
        "from tgmonitor.plugins.api import UiContribution\n"
        "class P:\n"
        "    manifest = None\n"
        "    ui_contribution = UiContribution('插件', 'nav_settings', lambda: None)\n"
        "    def activate(self, ctx): pass\n"
        "    def deactivate(self): pass\n"
    )
    _make_plugin_dir(tmp_path, "uiplug", body=body)
    # 写 capabilities = ["ui"] 覆盖默认的 ["events"]
    (tmp_path / "uiplug" / "plugin.toml").write_text(
        'id = "uiplug"\nname = "uiplug"\nmin_app_version = "3.0.0"\ncapabilities = ["ui"]\n',
        encoding="utf-8",
    )
    write_enabled(tmp_path, frozenset({"uiplug"}))
    host = PluginHost(tmp_path)
    host.activate_all(
        EventBus(),
        app_services={"messages": object(), "media": object(), "channels": object()},
        settings=Settings.for_test(),
    )
    contribution = _read_ui(host, "uiplug")
    assert contribution is not None
    assert contribution.nav_label == "插件"
    assert contribution.icon_name == "nav_settings"


def test_ui_contribution_ignored_without_capability(tmp_path) -> None:
    """未声明 ui 能力 → 即便插件有 ui_contribution 也不收集。"""
    body = (
        "from tgmonitor.plugins.api import UiContribution\n"
        "class P:\n"
        "    manifest = None\n"
        "    ui_contribution = UiContribution('插件', 'nav_settings', lambda: None)\n"
        "    def activate(self, ctx): pass\n"
        "    def deactivate(self): pass\n"
    )
    _make_plugin_dir(tmp_path, "nocap", body=body)  # 默认 capabilities = ["events"]
    write_enabled(tmp_path, frozenset({"nocap"}))
    host = PluginHost(tmp_path)
    host.activate_all(
        EventBus(),
        app_services={"messages": object(), "media": object(), "channels": object()},
        settings=Settings.for_test(),
    )
    assert _read_ui(host, "nocap") is None


def test_ui_contribution_bad_type_ignored(tmp_path) -> None:
    """ui_contribution 类型不符 → 忽略,插件仍 ACTIVE。"""
    body = (
        "class P:\n"
        "    manifest = None\n"
        "    ui_contribution = 'not a contribution'\n"
        "    def activate(self, ctx): pass\n"
        "    def deactivate(self): pass\n"
    )
    _make_plugin_dir(tmp_path, "badtype", body=body)
    (tmp_path / "badtype" / "plugin.toml").write_text(
        'id = "badtype"\nname = "badtype"\nmin_app_version = "3.0.0"\ncapabilities = ["ui"]\n',
        encoding="utf-8",
    )
    write_enabled(tmp_path, frozenset({"badtype"}))
    host = PluginHost(tmp_path)
    host.activate_all(
        EventBus(),
        app_services={"messages": object(), "media": object(), "channels": object()},
        settings=Settings.for_test(),
    )
    assert _read_ui(host, "badtype") is None
    assert host.records()["badtype"].state is PluginState.ACTIVE


# ---------- nav 层:add_item ----------


def test_nav_add_item_appends_after_builtin(qapp) -> None:
    """add_item 追加到内置 4 项之后,索引 = 4,信号正确。"""
    from tgmonitor.ui.nav_bar import VerticalNavBar

    nav = VerticalNavBar()
    assert len(nav._buttons) == 4
    idx = nav.add_item("nav_settings", "插件页")
    assert idx == 4
    assert len(nav._buttons) == 5
    seen: list[int] = []
    nav.current_changed.connect(seen.append)
    nav.add_item("nav_dashboard", "另一页")
    nav.set_current(4)
    assert seen == [4]
    assert nav._buttons[4]._active is True
    assert nav._buttons[0]._active is False


# ---------- 装配层:真实 MainWindow 注入 ----------


def test_main_window_injects_plugin_page(qapp, qloop, tmp_path, monkeypatch) -> None:
    """构造真实 MainWindow 时,插件页被注入 stack,nav 同索引追加。"""
    created: list[QWidget] = []

    def factory() -> QWidget:
        w = QWidget()
        created.append(w)
        return w

    fake = _FakeHost(
        {
            "plug_a": UiContribution("插件 A", "nav_settings", factory),
            "no_ui": None,
        }
    )
    monkeypatch.setattr("tgmonitor.plugins.host.PLUGIN_HOST", fake)

    win = build_real_main_window(qloop, tmp_path)
    assert len(created) == 1
    assert win.stack.count() == 5  # 内置 4 + 插件 1
    assert len(win.nav._buttons) == 5
    assert win.stack.widget(4) is created[0]
    win.close()


def test_main_window_bad_plugin_factory_isolated(qapp, qloop, tmp_path, monkeypatch) -> None:
    """插件 factory 抛错被隔离,其它页不受影响,窗口照常装配。"""

    def bad_factory() -> QWidget:
        raise RuntimeError("factory boom")

    fake = _FakeHost({"bad": UiContribution("坏插件", "nav_settings", bad_factory)})
    monkeypatch.setattr("tgmonitor.plugins.host.PLUGIN_HOST", fake)

    win = build_real_main_window(qloop, tmp_path)
    assert win.stack.count() == 4  # 坏页未注入
    assert len(win.nav._buttons) == 4
    win.close()
