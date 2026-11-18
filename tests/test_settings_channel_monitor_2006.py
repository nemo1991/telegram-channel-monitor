"""v2.0.6 频道管理归位契约 — 监听控件单实例迁入设置页。

- G1：全局恰好 1 个 ChannelWidget,父链在设置页内;
- G2：设置页存在「监听频道管理」分组且承载该控件;
- G3：MainWindow / ctx 仍持同一实例(控制器门控/同步接线零改动);
- G4：导航 3 项、stack 3 页,不再有独立频道页标题。
"""

from __future__ import annotations

from PySide6.QtWidgets import QGroupBox, QLabel

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.widgets.channel_widget import ChannelWidget
from tgmonitor.ui.widgets.settings_page import SettingsPage


def test_single_channel_widget_lives_in_settings(qapp, qloop, tmp_path):
    """G1：全窗口只有 1 个 ChannelWidget,且 parent 链在设置页内。"""
    win = build_real_main_window(qloop, tmp_path)
    panels = win.findChildren(ChannelWidget)
    assert len(panels) == 1

    settings = win.findChild(SettingsPage)
    assert settings is not None
    assert panels[0] is settings.channel_panel

    # parent 链必须走到 SettingsPage(而非独立频道页容器)
    node = panels[0].parent()
    chain: list[object] = []
    while node is not None:
        chain.append(node)
        if node is settings:
            break
        node = node.parent()
    assert settings in chain
    win.close()


def test_settings_has_channel_monitor_group(qapp, qloop, tmp_path):
    """G2：设置页有监听频道管理分组,承载 ChannelWidget。"""
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None
    group = settings.findChild(QGroupBox, "settingsChannelMonitorGroup")
    assert group is not None
    assert group.title() == "监听频道管理"
    assert group.findChild(ChannelWidget) is settings.channel_panel
    win.close()


def test_main_and_ctx_share_single_panel(qapp, qloop, tmp_path):
    """G3：MainWindow.channel_panel 与 ctx.channel_panel 指向同一实例。"""
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None
    panel = settings.channel_panel
    assert panel is not None
    assert win.channel_panel is panel
    assert win._ctx.channel_panel is panel
    win.close()


def test_no_standalone_channel_page(qapp, qloop, tmp_path):
    """G4：导航 3 项、stack 3 页,不再有独立「频道管理」页标题。"""
    win = build_real_main_window(qloop, tmp_path)
    assert len(win.nav._buttons) == 3
    assert win.stack.count() == 3
    all_titles = [lbl.text() for lbl in win.findChildren(QLabel)]
    assert "频道管理" not in all_titles
    win.close()
