"""v2.0.6 频道管理归位契约 — 监听控件单实例迁入设置页。

- G1：全局恰好 1 个 ChannelWidget,父链在设置页内;
- G2：设置页存在「监听频道管理」分组且承载该控件;
- G3：MainWindow / ctx 仍持同一实例(控制器门控/同步接线零改动);
- G4：导航 3 项、stack 3 页,不再有独立频道页标题。
"""

from __future__ import annotations

from pathlib import Path

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


def test_channel_widget_has_no_legacy_compat_aliases() -> None:
    """FR-09 L1:旧兼容桥接属性已由语义明确的公开句柄替代。"""
    source = (
        Path(__file__).resolve().parents[1] / "src/tgmonitor/ui/widgets/channel_widget.py"
    ).read_text(encoding="utf-8")
    legacy = (
        "lst_joined",
        "lst_monitored",
        "lbl_joined_count",
        "lbl_subs_count",
        "btn_refresh",
        "btn_sync",
        "sync_button",  # 2026-11-19:全量同步入口删除后不应回潮
    )
    for name in legacy:
        assert name not in source, f"旧桥接属性 {name} 回潮"
    for name in (
        "joined_list",
        "monitored_list",
        "joined_count_label",
        "monitored_count_label",
        "refresh_button",  # 左栏「同步」按钮（原刷新）
    ):
        assert f"self.{name}" in source, f"新公开句柄 {name} 缺失"


def test_full_sync_entry_removed_2006() -> None:
    """2026-11-19:桌面端「全量同步」入口整体移除，防止回潮。

    core `channel_sync` / CLI `tgmonitor sync` 保留；本守卫只锁 UI 入口：
    - ChannelWidget 不再 emit `sync_requested`
    - 下栏「已监听」无全量同步按钮
    - sync_flow_controller.py / sync_dialog.py 文件已删除
    """
    root = Path(__file__).resolve().parents[1]
    cw_src = (root / "src/tgmonitor/ui/widgets/channel_widget.py").read_text(encoding="utf-8")
    # 只锁代码定义，允许说明性注释里出现这些词（记录移除历史）
    assert "sync_requested = Signal" not in cw_src, "全量同步信号定义回潮"
    assert 'action_label="全量同步' not in cw_src, "全量同步按钮回潮"

    assert not (root / "src/tgmonitor/ui/controllers/sync_flow_controller.py").exists()
    assert not (root / "src/tgmonitor/ui/widgets/sync_dialog.py").exists()

    # 下栏构造使用 show_action=False（无按钮）
    assert "show_action=False" in cw_src
