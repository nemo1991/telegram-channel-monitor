"""PR #2 SearchBar QSS 主题化 + scope toggle 视觉反馈测试 — 2026-09-11 v1.7.5。

回归场景:
- SearchBar.init 不再调 setStyleSheet()(inline styleSheet 已删)
- scope toggle(🌐)checked 时刷 dynamic property `scopeActive="true"`
- `_update_scope_visual()` 让 QSS 立即重应用
- style.qss 浅色 + style_dark.qss 暗色都有 `#searchBar QToolButton[scopeActive="true"]`
  视觉区分 selector
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from tgmonitor.ui.widgets.search_bar import SearchBar


@pytest.fixture
def qapp() -> QApplication:
    """QApplication instance(单 widget 不需要 i18n)。"""
    app = QApplication.instance() or QApplication([])
    return app  # type: ignore[return-value]


def test_search_bar_no_inline_style(qapp: QApplication) -> None:
    """2026-09-11 v1.7.5:SearchBar.init 不调 setStyleSheet()(暗色主题破皮源)。"""
    bar = SearchBar()
    bar.show()
    qapp.processEvents()
    # Qt widget.styleSheet() 自身是空字符串(走全局 QSS)
    assert bar.styleSheet() == "", f"SearchBar 还有 inline styleSheet:{bar.styleSheet()[:80]!r}"


def test_search_bar_source_no_stylesheet_call() -> None:
    """静态检查:search_bar.py 源文件 init 内不调 setStyleSheet。"""
    src = Path("src/tgmonitor/ui/widgets/search_bar.py").read_text(encoding="utf-8")
    # 在 __init__ 内不能有 `self.setStyleSheet(`
    # 简单粗暴:扫全文 `self.setStyleSheet(`,确认没出现
    assert "self.setStyleSheet(" not in src, (
        "search_bar.py 还有 self.setStyleSheet() 调用 — 应迁到 style.qss"
    )


def test_scope_btn_has_dynamic_property_initialized_false(
    qapp: QApplication,
) -> None:
    """🌐 scope toggle 初始 dynamic property `scopeActive="false"`(让 QSS 应用 unchecked 样式)。"""
    bar = SearchBar()
    bar.show()
    qapp.processEvents()
    assert bar.scope_btn.property("scopeActive") == "false"


def test_scope_btn_property_changes_on_toggle(qapp: QApplication) -> None:
    """点 🌐 → property 切到 "true"。"""
    bar = SearchBar()
    bar.show()
    qapp.processEvents()
    bar.scope_btn.click()  # checked = True
    qapp.processEvents()
    assert bar.scope_btn.property("scopeActive") == "true"
    # 再点切回 false
    bar.scope_btn.click()
    qapp.processEvents()
    assert bar.scope_btn.property("scopeActive") == "false"


def test_scope_btn_emits_signal(qapp: QApplication) -> None:
    """点 🌐 → scope_changed signal emit True。"""
    bar = SearchBar()
    bar.show()
    qapp.processEvents()
    captured: list[bool] = []
    bar.scope_changed.connect(captured.append)
    bar.scope_btn.click()
    qapp.processEvents()
    assert captured == [True]


def test_qss_has_searchbar_block_light() -> None:
    """style.qss 浅色必须含 #searchBar selector(对称 dark)。"""
    text = Path("src/tgmonitor/ui/resources/style.qss").read_text(encoding="utf-8")
    assert "#searchBar" in text, "style.qss 缺 #searchBar selector"
    assert "QToolButton" in text
    assert "scopeActive" in text, "scope_btn checked 视觉反馈 selector 缺失"


def test_qss_has_searchbar_block_dark() -> None:
    """style_dark.qss 暗色必须含 #searchBar + scopeActive selector。"""
    text = Path("src/tgmonitor/ui/resources/style_dark.qss").read_text(encoding="utf-8")
    assert "#searchBar" in text
    assert "scopeActive" in text, "暗色 QSS 缺 scopeActive selector"


def test_qss_scope_active_selector_complete() -> None:
    """浅色 + 暗色 QSS 必须有 `[scopeActive="true"]` selector(checked 状态)。"""
    light = Path("src/tgmonitor/ui/resources/style.qss").read_text(encoding="utf-8")
    dark = Path("src/tgmonitor/ui/resources/style_dark.qss").read_text(encoding="utf-8")
    # 完整 selector 形态
    selector = 'QToolButton[scopeActive="true"]'
    assert selector in light, "浅色 QSS 缺完整 selector"
    assert selector in dark, "暗色 QSS 缺完整 selector"


def test_update_scope_visual_repolishes(qapp: QApplication) -> None:
    """`_update_scope_visual()` 不会崩(unpolish/repolish 调用)。"""
    bar = SearchBar()
    bar.show()
    qapp.processEvents()
    # 直接调,不通过 toggle 信号(避免 tooltip 副作用)
    bar._update_scope_visual(True)  # noqa: SLF001 — 测试私有方法
    bar._update_scope_visual(False)  # noqa: SLF001
    qapp.processEvents()
    assert bar.scope_btn.property("scopeActive") == "false"
