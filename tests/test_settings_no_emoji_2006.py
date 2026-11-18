"""v2.0.6 设置页去 emoji 契约 — 静态扫描 settings_page.py。

FR-04：6 个分组标题 + 4 处测试连接弹窗 + 相关 docstring 的 emoji 清零。
保留 objectName=pageTitle 与全局 #pageTitle QSS(另有契约守护)。
"""

from __future__ import annotations

import re
from pathlib import Path

# 装饰性 emoji 码点区间(与 PRD FR-04 口径一致)
_EMOJI = re.compile(
    "["
    "\U0001f300-\U0001faff"  # 杂项图形/表情
    "\u2600-\u27bf"  # 杂项符号 + 装饰符号
    "\ufe0f"  # 变体选择符
    "]"
)

_SETTINGS_PAGE = Path(__file__).resolve().parents[1] / "src/tgmonitor/ui/widgets/settings_page.py"


def test_settings_page_has_no_emoji():
    """FR-04：settings_page.py 全文零 emoji(UI 文案 + docstring)。"""
    text = _SETTINGS_PAGE.read_text(encoding="utf-8")
    hits = [
        (i + 1, line.strip()) for i, line in enumerate(text.splitlines()) if _EMOJI.search(line)
    ]
    assert hits == [], f"settings_page.py 仍有 emoji: {hits}"


def test_group_titles_are_plain_text(qapp, qloop, tmp_path):
    """FR-04：各分组标题为纯文字(无符号前缀),pageTitle 结构保留。"""
    from tests.fixtures._real_window import build_real_main_window
    from tgmonitor.ui.widgets.settings_page import SettingsPage

    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None

    from PySide6.QtWidgets import QGroupBox

    titles = [g.title() for g in settings.findChildren(QGroupBox)]
    for title in titles:
        assert not _EMOJI.search(title), f"分组标题仍有 emoji: {title!r}"
    assert "账户凭证" in titles
    assert "监听频道管理" in titles

    # pageTitle 结构保留(全局 QSS 规则命中)
    assert settings.header_label.objectName() == "pageTitle"
    win.close()
