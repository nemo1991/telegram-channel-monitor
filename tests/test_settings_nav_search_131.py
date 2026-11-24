"""v2.0.10 issue #131 — 设置页左侧分组导航 + 搜索过滤契约。

- 左侧导航项 = 7 个分组标题(账户/监听频道/代理/存储/对象存储/策略/同步);
- 点选某项:右侧只显示该分组,其余隐藏;
- 搜索框:按分组标题 / 字段 label / 字段 key 过滤导航项;
- 当前项被过滤掉时自动跳到首个可见项;
- 底部「保存」始终可见。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QGroupBox,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QWidget,
)

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.widgets.settings_page import SettingsPage

_QSS_PATH = Path(__file__).resolve().parents[1] / "src/tgmonitor/ui/resources/style.qss"

_EXPECTED_TITLES = (
    "账户凭证",
    "监听频道管理",
    "网络代理 (Proxy)",
    "消息存储 (Database)",
    "对象存储 (ObjectStore)",
    "策略",
    "同步参数",
)


def _nav_titles(settings: SettingsPage) -> list[str]:
    return [settings.nav_list.item(i).text() for i in range(settings.nav_list.count())]


def _visible_groups(settings: SettingsPage) -> list[QGroupBox]:
    """用 isHidden() 判断导航切换效果(窗口未 show 时 isVisible() 恒 False)。"""
    return [g for g in settings.findChildren(QGroupBox) if not g.isHidden()]


def test_nav_has_all_group_titles(qapp, qloop, tmp_path):
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None
    assert settings.findChild(QListWidget, "settingsNav") is settings.nav_list
    assert settings.findChild(QLineEdit, "settingsSearchInput") is settings.search_edit
    assert tuple(_nav_titles(settings)) == _EXPECTED_TITLES
    win.close()


def test_nav_switch_shows_only_selected_group(qapp, qloop, tmp_path):
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None

    settings.nav_list.setCurrentRow(2)  # 网络代理
    visible = _visible_groups(settings)
    assert len(visible) == 1
    assert visible[0].objectName() == "settingsSection"
    assert "代理" in visible[0].title()

    settings.nav_list.setCurrentRow(1)  # 监听频道管理
    visible = _visible_groups(settings)
    assert len(visible) == 1
    assert visible[0].objectName() == "settingsChannelMonitorGroup"
    win.close()


def test_search_filters_nav_by_title_and_field_key(qapp, qloop, tmp_path):
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None

    def visible_titles() -> list[str]:
        return [
            settings.nav_list.item(i).text()
            for i in range(settings.nav_list.count())
            if not settings.nav_list.item(i).isHidden()
        ]

    # 标题命中
    settings.search_edit.setText("代理")
    assert visible_titles() == ["网络代理 (Proxy)"]
    # 字段 label 命中(api_id 属于账户凭证)
    settings.search_edit.setText("api_id")
    assert visible_titles() == ["账户凭证"]
    # 中文字段标签命中
    settings.search_edit.setText("分页间隔")
    assert visible_titles() == ["同步参数"]
    # 英文 key 命中
    settings.search_edit.setText("proxy")
    assert visible_titles() == ["网络代理 (Proxy)"]
    # 清空恢复
    settings.search_edit.clear()
    assert tuple(visible_titles()) == _EXPECTED_TITLES
    win.close()


def test_search_switches_when_current_row_hidden(qapp, qloop, tmp_path):
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None

    settings.nav_list.setCurrentRow(5)  # 策略
    settings.search_edit.setText("代理")  # 策略项被过滤掉
    assert settings.nav_list.currentRow() == 2
    assert "代理" in _visible_groups(settings)[0].title()
    win.close()


def test_apply_button_stays_visible(qapp, qloop, tmp_path):
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None
    apply_btn = settings.findChild(QPushButton, "primaryBtn")
    assert apply_btn is not None and not apply_btn.isHidden()
    settings.nav_list.setCurrentRow(6)
    assert not apply_btn.isHidden()
    win.close()


def test_qss_has_nav_selectors() -> None:
    qss = _QSS_PATH.read_text(encoding="utf-8")
    for selector in (
        "QWidget#settingsNavPane",
        "QLineEdit#settingsSearchInput",
        "QListWidget#settingsNav",
        "QListWidget#settingsNav::item:selected",
        "QWidget#settingsDetailPane",
    ):
        assert selector in qss


def test_nav_pane_reaches_page_bottom(qapp, qloop, tmp_path):
    """左导航白条铺满整页高度,不与底部按钮栏之间留灰底断层。"""
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None
    nav_pane = settings.findChild(QWidget, "settingsNavPane")
    assert nav_pane is not None
    # 导航面板底边 = 设置页底边(按钮栏已移入右侧 detail,不再独占 root 一行)
    assert nav_pane.geometry().bottom() == settings.height() - 1
    win.close()


def test_channel_monitor_group_fills_scroll_area(qapp, qloop, tmp_path):
    """监听频道管理卡片弹性填满滚动区,底部到「保存」只留一小段间距。"""
    win = build_real_main_window(qloop, tmp_path)
    win.resize(1200, 800)
    stack = win.findChild(QStackedWidget)
    settings = win.findChild(SettingsPage)
    assert stack is not None
    stack.setCurrentWidget(settings)
    assert settings is not None
    settings.nav_list.setCurrentRow(1)  # 监听频道管理
    qapp.processEvents()
    qapp.processEvents()

    gb = settings.findChild(QGroupBox, "settingsChannelMonitorGroup")
    assert gb is not None
    assert gb.minimumHeight() >= 560
    assert gb.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Expanding
    assert gb.height() > 620, "高窗口下卡片应吃掉滚动区余量,而非停在最小高度"
    gap = settings.btn_apply.geometry().top() - gb.geometry().bottom()
    assert 20 <= gap <= 80, f"卡片底边到保存按钮间距应适中,实际 {gap}px"
    win.close()


def test_other_groups_stay_compact(qapp, qloop, tmp_path):
    """非监听分组不因尾部 stretch 消失而被拉高(保持 sizeHint 紧凑排列)。"""
    win = build_real_main_window(qloop, tmp_path)
    win.resize(1200, 800)
    stack = win.findChild(QStackedWidget)
    settings = win.findChild(SettingsPage)
    assert stack is not None
    stack.setCurrentWidget(settings)
    assert settings is not None
    settings.nav_list.setCurrentRow(0)  # 账户凭证
    qapp.processEvents()
    qapp.processEvents()

    visible = [gb for gb in settings.findChildren(QGroupBox) if not gb.isHidden()]
    assert len(visible) == 1
    assert visible[0].height() < 300, "账户凭证卡片不应被拉伸到整页高度"
    win.close()


def test_apply_button_label_is_save(qapp, qloop, tmp_path):
    """底栏按钮文案为「保存」(行为仍是写盘 + 热重载)。"""
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None
    assert settings.btn_apply.text() == "保存"
    win.close()
