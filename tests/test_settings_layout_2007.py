"""v2.0.7 FR-04/FR-05/FR-10 契约 — 设置页白底、无页标题、卡片内标题、布局规整。

- 页面级「设置」标题删除,页面与滚动内容白底;
- 6 个 FormSection 挂 settingsSection objectName,标题由 QSS 画在卡片内左上;
- 监听频道管理分组保持 settingsChannelMonitorGroup,同样内移;
- 全局 QGroupBox 规则不动(大盘 dashPanel 与设置页各走专属选择器);
- 卡片内边距 / 表单行距 / 分组间距统一。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QFormLayout, QGroupBox, QVBoxLayout, QWidget

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.widgets.settings_page import SettingsPage

_QSS_PATH = Path(__file__).resolve().parents[1] / "src/tgmonitor/ui/resources/style.qss"


def test_no_page_title_and_white_background(qapp, qloop, tmp_path):
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None
    assert settings.objectName() == "settingsPage"
    assert not hasattr(settings, "header_label")

    content = settings.findChild(QWidget, "settingsScrollContent")
    assert content is not None
    win.close()


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


def test_section_spacing_is_unified(qapp, qloop, tmp_path):
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None

    form_sections = [
        g for g in settings.findChildren(QGroupBox) if g.objectName() == "settingsSection"
    ]
    for gb in form_sections:
        vbox = gb.layout()
        assert isinstance(vbox, QVBoxLayout)
        m = vbox.contentsMargins()
        assert (m.left(), m.top(), m.right(), m.bottom()) == (16, 40, 16, 16)
        form = vbox.itemAt(0).layout()
        assert isinstance(form, QFormLayout)
        assert form.spacing() == 12
    win.close()


def test_qss_white_background_and_in_card_titles() -> None:
    qss = _QSS_PATH.read_text(encoding="utf-8")
    assert "#settingsPage," in qss
    assert "#settingsPage #settingsScrollContent" in qss
    assert "background: #ffffff;" in qss[qss.index("#settingsPage,") :]

    block = qss[qss.index("QGroupBox#settingsSection,") :]
    assert "QGroupBox#settingsSection::title" in block
    assert "QGroupBox#settingsChannelMonitorGroup::title" in block
    title_rule = qss[qss.index("QGroupBox#settingsSection::title") :]
    assert "subcontrol-origin: padding;" in title_rule[:400]
    assert "top: 10px;" in title_rule[:400]
    assert "left: 16px;" in title_rule[:400]

    # 全局 QGroupBox 规则不动(其它面板/测试依赖)
    global_block = qss[qss.index("QGroupBox {") : qss.index("QGroupBox::title")]
    assert "margin-top: 14px" in global_block
