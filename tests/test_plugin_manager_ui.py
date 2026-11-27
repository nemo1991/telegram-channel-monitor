"""插件管理 UI 测试 — ADR-001 阶段 4 插件管理页。

覆盖:
- 设置页「插件管理」按钮存在,点击打开对话框(不弹 exec 阻塞测试)
- 对话框列表呈现 / 状态标签 / 详情 / 启用停用切换
- KV 表格增删行 + 保存(类型保留)
- 状态订阅自动重绘 + 关闭退订
- 插件 UI 页增量同步:停用 A 不重建 B 的 widget

手法:monkeypatch PLUGIN_HOST 为真实 PluginHost 实例(带本地插件目录),
UI 对话框 / 真实 MainWindow 走离屏 Qt。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PySide6.QtWidgets import QDialog, QGroupBox, QWidget

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.core.config import Settings
from tgmonitor.core.events import EventBus
from tgmonitor.plugins.api import UiContribution
from tgmonitor.plugins.config import write_enabled
from tgmonitor.plugins.host import PluginHost, PluginState


@pytest.fixture(autouse=True)
def _no_entry_points(monkeypatch):
    monkeypatch.setattr("tgmonitor.plugins.loader.discover_entry_points", lambda: [])


@pytest.fixture
def services():
    return {"messages": AsyncMock(), "media": AsyncMock(), "channels": AsyncMock()}


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


def _boot(host: PluginHost, services, *, enabled: frozenset[str] = frozenset()) -> None:
    write_enabled(host.plugins_dir, enabled)
    host.activate_all(EventBus(), app_services=services, settings=Settings.for_test())


# ---------- 设置页入口 ----------


def test_settings_page_has_plugin_manager_button(qapp, qloop, tmp_path) -> None:
    """设置页账户分组内有「插件管理」按钮,不新增 QGroupBox。"""
    win = build_real_main_window(qloop, tmp_path)
    btn = win.settings_page.findChild(QWidget, "settingsPluginManagerBtn")
    assert btn is not None
    assert len(win.settings_page.findChildren(QGroupBox)) == 7
    win.close()


def test_plugin_manager_button_opens_dialog(qapp, qloop, tmp_path, monkeypatch) -> None:
    """点按钮 → 打开对话框(exec 替换成 no-op,避免阻塞)。"""
    win = build_real_main_window(qloop, tmp_path)
    opened: list[QDialog] = []

    def fake_exec(self):
        opened.append(self)
        return 0

    monkeypatch.setattr(
        "tgmonitor.ui.widgets.plugin_manager_dialog.PluginManagerDialog.exec",
        fake_exec,
    )
    win.settings_page.btn_plugin_manager.click()
    assert len(opened) == 1
    assert opened[0].windowTitle() == "插件管理"
    opened[0].close()
    win.close()


# ---------- 对话框交互 ----------


def test_dialog_lists_plugins_and_states(qapp, services, tmp_path) -> None:
    """对话框列出全部插件 + 状态标签,选中显示详情。"""
    _make_plugin(tmp_path, "on")
    _make_plugin(tmp_path, "off")
    host = PluginHost(tmp_path)
    _boot(host, services, enabled=frozenset({"on"}))

    from tgmonitor.ui.widgets.plugin_manager_dialog import PluginManagerDialog

    dlg = PluginManagerDialog(host)
    assert dlg.plugin_list.count() == 2
    labels = [dlg.plugin_list.item(i).text() for i in range(dlg.plugin_list.count())]
    assert any("已启用" in t for t in labels)
    assert any("已停用" in t for t in labels)
    dlg.close()


def test_dialog_toggle_enable_and_disable(qapp, services, tmp_path) -> None:
    """对话框点「启用」→ host ACTIVE + on_changed 回调;再点「停用」→ DISABLED。"""
    _make_plugin(tmp_path, "toggle")
    host = PluginHost(tmp_path)
    _boot(host, services)
    from tgmonitor.ui.widgets.plugin_manager_dialog import PluginManagerDialog

    changes: list[int] = []
    dlg = PluginManagerDialog(host, on_changed=lambda: changes.append(1))
    # 选中 toggle(列表第一项)
    dlg.plugin_list.setCurrentRow(0)
    assert dlg.btn_toggle.text() == "启用"
    dlg.btn_toggle.click()
    assert host.records()["toggle"].state is PluginState.ACTIVE
    assert dlg.btn_toggle.text() == "停用"
    assert len(changes) == 1

    dlg.btn_toggle.click()
    assert host.records()["toggle"].state is PluginState.DISABLED
    assert len(changes) == 2
    dlg.close()


def test_dialog_config_kv_save_preserves_types(qapp, services, tmp_path) -> None:
    """KV 表格加行 → 保存 → config.json 按 JSON 字面量保类型。"""
    _make_plugin(tmp_path, "conf")
    host = PluginHost(tmp_path)
    _boot(host, services, enabled=frozenset({"conf"}))
    from tgmonitor.ui.widgets.plugin_manager_dialog import PluginManagerDialog

    dlg = PluginManagerDialog(host)
    dlg.plugin_list.setCurrentRow(0)
    # 已有 0 行 → 加两行
    dlg._on_add_row()
    dlg.config_table.item(0, 0).setText("count")
    dlg.config_table.item(0, 1).setText("42")
    dlg._on_add_row()
    dlg.config_table.item(1, 0).setText("enabled")
    dlg.config_table.item(1, 1).setText("true")
    dlg._on_add_row()
    dlg.config_table.item(2, 0).setText("name")
    dlg.config_table.item(2, 1).setText("hello")
    dlg._on_save_config()

    config = host.config_for("conf")
    assert config == {"count": 42, "enabled": True, "name": "hello"}
    assert dlg.lbl_hint.text() == "配置已保存"
    dlg.close()


def test_dialog_remove_row(qapp, services, tmp_path) -> None:
    """删除行后保存,该 key 不再写入配置。"""
    from tgmonitor.plugins.config import write_all_config

    write_all_config(tmp_path, {"conf": {"a": 1, "b": 2}})
    _make_plugin(tmp_path, "conf")
    host = PluginHost(tmp_path)
    _boot(host, services, enabled=frozenset({"conf"}))
    from tgmonitor.ui.widgets.plugin_manager_dialog import PluginManagerDialog

    dlg = PluginManagerDialog(host)
    dlg.plugin_list.setCurrentRow(0)
    assert dlg.config_table.rowCount() == 2
    dlg.config_table.setCurrentCell(0, 0)
    dlg._on_remove_row()
    dlg._on_save_config()
    assert host.config_for("conf") == {"b": 2}
    dlg.close()


def test_dialog_unsubscribes_on_close(qapp, services, tmp_path) -> None:
    """关闭对话框后退订 host 状态通知。"""
    _make_plugin(tmp_path, "sub")
    host = PluginHost(tmp_path)
    _boot(host, services)
    from tgmonitor.ui.widgets.plugin_manager_dialog import PluginManagerDialog

    dlg = PluginManagerDialog(host)
    assert len(host._state_listeners) == 1
    dlg.close()
    assert len(host._state_listeners) == 0


# ---------- UI 页增量同步 ----------


def test_hotswap_ui_page_incremental(qapp, qloop, tmp_path, services, monkeypatch) -> None:
    """停用 A 不重建 B 的页面 widget(增量同步,保留状态)。"""
    created: dict[str, list[QWidget]] = {"a": [], "b": []}

    def factory_a() -> QWidget:
        w = QWidget()
        w.setObjectName("page_a")
        created["a"].append(w)
        return w

    def factory_b() -> QWidget:
        w = QWidget()
        w.setObjectName("page_b")
        created["b"].append(w)
        return w

    # 两个 ACTIVE 插件各贡献一页
    _make_plugin(tmp_path, "a", caps='["ui"]')
    _make_plugin(tmp_path, "b", caps='["ui"]')
    host = PluginHost(tmp_path)
    _boot(host, services, enabled=frozenset({"a", "b"}))

    monkeypatch.setattr("tgmonitor.plugins.host.PLUGIN_HOST", host)
    # 直接把 UiContribution 注入记录(避免为测试重写插件模块文件)
    for pid, factory in (("a", factory_a), ("b", factory_b)):
        old = host.records()[pid]
        object.__setattr__(
            old,
            "ui_contribution",
            UiContribution(f"页{pid}", "nav_settings", factory),
        )

    win = build_real_main_window(qloop, tmp_path)
    assert win.stack.count() == 6  # 内置 4 + 插件 2
    assert len(created["a"]) == 1
    assert len(created["b"]) == 1

    b_widget_before = win.stack.widget(5)
    # 停用 a → 触发 UI 同步
    host.disable("a")
    win.settings_page.plugins_changed.emit()

    assert win.stack.count() == 5
    assert len(win.nav._buttons) == 5
    assert len(created["a"]) == 1  # a 未重建
    assert len(created["b"]) == 1  # b 未重建
    assert win.stack.widget(4) is b_widget_before  # b 页对象保持
    win.close()
