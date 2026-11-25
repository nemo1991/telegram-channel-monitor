"""v2.0.6 频道管理归位契约 — 监听控件单实例迁入设置页。

- G1：全局恰好 1 个 ChannelWidget,父链在设置页内;
- G2：设置页存在「监听频道管理」分组且承载该控件;
- G3：MainWindow / ctx 仍持同一实例(控制器门控/同步接线零改动);
- G4：导航 3 项、stack 3 页,不再有独立频道页标题。
"""

from __future__ import annotations

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.widgets.settings_page import SettingsPage


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
