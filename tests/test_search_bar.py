"""SearchBar widget 测试 — 2026-09-02 v1.5.2 PR #B5。

覆盖:
- text_changed 在输入时 emit(已有基础功能)
- date_changed 在 date panel 变化时 emit(新功能)
- clear() 同时重置 text + dates + 折叠面板
- 高级按钮 toggle 控制 date panel 可见性
"""

from __future__ import annotations

import os
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QDate, QDateTime, QTime  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.ui.widgets.search_bar import SearchBar  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _qdt(year: int, month: int, day: int, h: int = 0, m: int = 0) -> QDateTime:
    """helper:构造 QDateTime,跨平台避免时区混淆。"""
    return QDateTime(QDate(year, month, day), QTime(h, m, 0))


class _SignalSpy:
    """轻量级 Qt signal 监听器 — 不依赖 pytest-qt(qtbot fixture)。

    用法:
        spy = _SignalSpy()
        bar.text_changed.connect(spy.slot)
        bar.edit.setText("hello")
        QApplication.processEvents()
        assert spy.args == ["hello"]
    """

    def __init__(self) -> None:
        self.args: list[object] = []

    def slot(self, *args: object) -> None:
        self.args.append(tuple(args) if len(args) > 1 else args[0])


def test_search_bar_initial_state(qapp):
    """PR #B5:新建 SearchBar → text 空 / date panel 隐藏 / adv 未选中。"""
    bar = SearchBar()
    assert bar.text() == ""
    assert bar.date_range() == (None, None)
    assert bar.adv_btn.isChecked() is False


def test_search_bar_text_changed_fires(qapp):
    """PR #B5:text_changed 在 QLineEdit 输入时 emit(沿用旧行为)。"""
    bar = SearchBar()
    spy = _SignalSpy()
    bar.text_changed.connect(spy.slot)
    bar.edit.setText("hello")
    QApplication.processEvents()
    assert "hello" in spy.args


def test_search_bar_clear_resets_text(qapp):
    """PR #B5:clear() 清空 text + 隐藏 btn_clear。"""
    bar = SearchBar()
    bar.edit.setText("abc")
    QApplication.processEvents()
    # isHidden 是 setVisible 的反向,独立于 widget 是否在 window 中显示
    assert bar.btn_clear.isHidden() is False
    bar.clear()
    QApplication.processEvents()
    assert bar.text() == ""
    assert bar.btn_clear.isHidden() is True


def test_search_bar_date_changed_fires_on_datetime_change(qapp):
    """PR #B5:date_from / date_to 任一变化 → date_changed signal emit。"""
    bar = SearchBar()
    bar.adv_btn.setChecked(True)
    QApplication.processEvents()
    spy = _SignalSpy()
    bar.date_changed.connect(spy.slot)

    target_dt = _qdt(2026, 1, 1, 10, 30)
    bar.dt_from.setDateTime(target_dt)
    QApplication.processEvents()

    # 至少 emit 一次,(from_dt, None) — from 是 datetime,to 仍是 None(没动)
    assert len(spy.args) >= 1
    last_f, last_t = spy.args[-1]
    assert last_f == datetime(2026, 1, 1, 10, 30, 0)
    assert last_t is None


def test_search_bar_clear_resets_dates(qapp):
    """PR #B5:clear() 重置 dt_from / dt_to 到「不限」状态 + 折叠面板。

    「不限」= setDateTime(minimumDateTime) 让 specialValueText 触发,
    `_date_to_python` 检测到后返 None。
    """
    bar = SearchBar()
    bar.adv_btn.setChecked(True)
    bar.dt_from.setDateTime(_qdt(2026, 1, 1))
    bar.dt_to.setDateTime(_qdt(2026, 1, 4))
    QApplication.processEvents()
    # 此时 date_range 应为有值
    f, t = bar.date_range()
    assert f is not None and t is not None

    bar.clear()
    QApplication.processEvents()
    # clear() 后 date_range 应回到 (None, None)
    assert bar.date_range() == (None, None)
    # 折叠面板隐藏
    assert bar.adv_btn.isChecked() is False


def test_search_bar_advanced_panel_toggle_visibility(qapp):
    """PR #B5:`📅` 按钮 toggle → 控制 date_panel 可见性。

    注:`isVisible()` 在 widget 没 parent + 未 show 时常返 False;改用
    `isHidden()` 检查预期隐藏状态(独立于 widget 是否在 window 中显示)。
    """
    bar = SearchBar()
    spy = _SignalSpy()
    bar.adv_btn.toggled.connect(spy.slot)

    # 初始:unchecked → date_panel 隐藏
    assert bar._date_panel.isHidden() is True
    bar.adv_btn.setChecked(True)
    QApplication.processEvents()
    assert bar._date_panel.isHidden() is False
    assert spy.args[-1] is True

    bar.adv_btn.setChecked(False)
    QApplication.processEvents()
    assert bar._date_panel.isHidden() is True
    assert spy.args[-1] is False


def test_search_bar_date_range_unlimited_returns_none(qapp):
    """PR #B5:dt_from / dt_to = minimumDateTime(1900-01-01)→ date_range() 返 None。"""
    bar = SearchBar()
    bar.adv_btn.setChecked(True)
    # 默认就是 1900-01-01(不限)
    assert bar.date_range() == (None, None)


def test_search_bar_date_range_partial(qapp):
    """PR #B5:只设 from / 只设 to → 另一边返 None。"""
    bar = SearchBar()
    bar.adv_btn.setChecked(True)
    bar.dt_from.setDateTime(_qdt(2026, 1, 1))
    QApplication.processEvents()
    f, t = bar.date_range()
    assert f == datetime(2026, 1, 1, 0, 0, 0)
    assert t is None


def test_search_bar_text_strip(qapp):
    """PR #B5:`text()` strip 首尾空白 — 与 v1.5.0 PR #A5 一致。"""
    bar = SearchBar()
    bar.edit.setText("  hello  ")
    QApplication.processEvents()
    assert bar.text() == "hello"


# ============================================================
# 2026-09-03 v1.5.3 PR #D2:scope toggle(已订阅 / 全部)
# ============================================================


def test_search_bar_initial_scope_is_subscribed(qapp):
    """PR #D2:新建 SearchBar → scope 默认 False(已订阅)。"""
    bar = SearchBar()
    assert bar.scope() is False


def test_search_bar_scope_changed_fires_on_toggle(qapp):
    """PR #D2:toggle scope_btn → emit scope_changed(True)。"""
    bar = SearchBar()
    spy = _SignalSpy()
    bar.scope_changed.connect(spy.slot)
    bar.scope_btn.setChecked(True)
    QApplication.processEvents()
    assert spy.args[-1] is True


def test_search_bar_scope_in_clear_resets(qapp):
    """PR #D2:clear() 重置 scope 回 False(已订阅默认)。"""
    bar = SearchBar()
    bar.scope_btn.setChecked(True)
    QApplication.processEvents()
    assert bar.scope() is True
    bar.clear()
    QApplication.processEvents()
    assert bar.scope() is False


def test_search_bar_scope_btn_tooltip_set(qapp):
    """PR #D2:scope_btn tooltip 含 "已订阅" / "全部" 字样提示用户。"""
    bar = SearchBar()
    tooltip = bar.scope_btn.toolTip()
    assert "已订阅" in tooltip
    assert "全部" in tooltip


# ============================================================
# 2026-09-14 v1.7.5 PR #8:★/🏷/📌 3 filter toggle 测试
# ============================================================


def test_search_bar_filter_toggles_default_false(qapp):
    """PR #8:新建 SearchBar → 3 filter toggle 默认 False(不窄化)。"""
    bar = SearchBar()
    assert bar.is_favorite_active() is False
    assert bar.is_tag_active() is False
    assert bar.is_pinned_active() is False


def test_search_bar_favorite_toggled_fires_signal(qapp):
    """PR #8:click ★ favorite_btn → emit favorite_toggled(True)。"""
    bar = SearchBar()
    spy = _SignalSpy()
    bar.favorite_toggled.connect(spy.slot)
    bar.favorite_btn.setChecked(True)
    QApplication.processEvents()
    assert spy.args[-1] is True
    assert bar.is_favorite_active() is True


def test_search_bar_tag_toggled_fires_signal(qapp):
    """PR #8:click 🏷 tag_btn → emit tag_toggled(True)。"""
    bar = SearchBar()
    spy = _SignalSpy()
    bar.tag_toggled.connect(spy.slot)
    bar.tag_btn.setChecked(True)
    QApplication.processEvents()
    assert spy.args[-1] is True
    assert bar.is_tag_active() is True


def test_search_bar_pinned_toggled_fires_signal(qapp):
    """PR #8:click 📌 pinned_btn → emit pinned_toggled(True)。"""
    bar = SearchBar()
    spy = _SignalSpy()
    bar.pinned_toggled.connect(spy.slot)
    bar.pinned_btn.setChecked(True)
    QApplication.processEvents()
    assert spy.args[-1] is True
    assert bar.is_pinned_active() is True


def test_search_bar_filter_toggles_independent(qapp):
    """PR #8:3 toggle 互不影响 — 切 ★ 不应触发 🏷 / 📌。"""
    bar = SearchBar()
    fav_spy = _SignalSpy()
    tag_spy = _SignalSpy()
    pin_spy = _SignalSpy()
    bar.favorite_toggled.connect(fav_spy.slot)
    bar.tag_toggled.connect(tag_spy.slot)
    bar.pinned_toggled.connect(pin_spy.slot)

    bar.favorite_btn.setChecked(True)
    QApplication.processEvents()
    assert fav_spy.args == [True]
    # tag / pinned 不应被触发
    assert tag_spy.args == []
    assert pin_spy.args == []


def test_search_bar_clear_resets_filter_toggles(qapp):
    """PR #8:clear() 重置 ★/🏷/📌 → 全 False。"""
    bar = SearchBar()
    bar.favorite_btn.setChecked(True)
    bar.tag_btn.setChecked(True)
    bar.pinned_btn.setChecked(True)
    assert bar.is_favorite_active() is True
    assert bar.is_tag_active() is True
    assert bar.is_pinned_active() is True

    bar.clear()
    QApplication.processEvents()
    assert bar.is_favorite_active() is False
    assert bar.is_tag_active() is False
    assert bar.is_pinned_active() is False


def test_search_bar_filter_toggle_dynamic_property(qapp):
    """PR #8:toggle 切 checked → dynamic property "favoriteActive" / "tagActive" /
    "pinnedActive" 同步切换(配合 QSS 应用高亮样式)。
    """
    bar = SearchBar()
    assert bar.favorite_btn.property("favoriteActive") == "false"
    assert bar.tag_btn.property("tagActive") == "false"
    assert bar.pinned_btn.property("pinnedActive") == "false"

    bar.favorite_btn.setChecked(True)
    QApplication.processEvents()
    assert bar.favorite_btn.property("favoriteActive") == "true"

    bar.favorite_btn.setChecked(False)
    QApplication.processEvents()
    assert bar.favorite_btn.property("favoriteActive") == "false"


def test_search_bar_filter_toggle_tooltips_set(qapp):
    """PR #8:3 toggle tooltip 含功能描述(中文字)。"""
    bar = SearchBar()
    assert "收藏" in bar.favorite_btn.toolTip()
    assert "标签" in bar.tag_btn.toolTip()
    assert "置顶" in bar.pinned_btn.toolTip()
