"""v2.0.7 FR-04/FR-05/FR-10 契约 — 设置页白底、无页标题、卡片内标题、布局规整。

- 页面级「设置」标题删除,页面与滚动内容白底;
- 6 个 FormSection 挂 settingsSection objectName,标题由 QSS 画在卡片内左上;
- 监听频道管理分组保持 settingsChannelMonitorGroup,同样内移;
- 全局 QGroupBox 规则不动(大盘 dashPanel 与设置页各走专属选择器);
- 卡片内边距 / 表单行距 / 分组间距统一。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QGroupBox

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.widgets.settings_page import SettingsPage

_QSS_PATH = Path(__file__).resolve().parents[1] / "src/tgmonitor/ui/resources/style.qss"


def test_all_sections_have_dedicated_object_names(qapp, qloop, tmp_path):
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None

    groups = settings.findChildren(QGroupBox)
    assert len(groups) == 7
    form_sections = [g for g in groups if g.objectName() == "settingsSection"]
    monitor = [g for g in groups if g.objectName() == "settingsChannelMonitorGroup"]
    assert len(form_sections) == 6
    assert len(monitor) == 1
    assert not any(g.objectName() == "dashPanel" for g in groups)
    win.close()
