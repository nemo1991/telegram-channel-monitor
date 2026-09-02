"""i18n bootstrap 测试 — 2026-09-03 v1.5.3 PR #D3。

覆盖:
- `install_translator` 装 gettext 域 + Qt translator
- locale 强制为 zh_CN
- fallback 不抛(找不到 .qm 不 raise)
- `self.tr("foo")` 在 zh_CN 下返 "foo"
- `i18n.get_i18n_dir()` 返回正确路径
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QLocale  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from tgmonitor.i18n import get_i18n_dir, install_translator  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_install_translator_sets_default_locale_to_zh_cn(qapp):
    """PR #D3:`install_translator(qt_app)` → `QLocale().name() == "zh_CN"`。"""
    install_translator(qapp)
    assert QLocale().name().startswith("zh_CN"), f"默认 locale 应是 zh_CN,got {QLocale().name()}"


def test_install_translator_fallback_for_missing_locale(qapp):
    """PR #D3:不存在的 locale 不抛 — log + 原文 fallback。"""
    # 找不到的 locale "fr_FR" 应不 raise
    install_translator(qapp, locale="fr_FR")
    # self.tr("foo") 应返 "foo"(原文)
    label = QLabel()
    label.setText(label.tr("foo"))
    assert label.text() == "foo"


def test_install_translator_idempotent(qapp):
    """PR #D3:重复装不抛 — 多次 install_translator 安全。"""
    install_translator(qapp)
    install_translator(qapp)  # 第二次应不挂
    install_translator(qapp, locale="zh_CN")
    assert QLocale().name().startswith("zh_CN")


def test_get_i18n_dir_returns_existing_path():
    """PR #D3:`get_i18n_dir()` 返回 i18n 资源目录,且目录存在。"""
    i18n_dir = get_i18n_dir()
    assert isinstance(i18n_dir, Path)
    assert i18n_dir.exists()
    assert i18n_dir.is_dir()
    assert i18n_dir.name == "i18n"


def test_default_zh_cn_tr_returns_original(qapp):
    """PR #D3:zh_CN 默认翻译 = 原文(zh_CN .ts 缺 msgstr 时 fallback)。

    本测试代表关键 invariant:zh_CN locale 下,所有 `self.tr("...")`
    调用都返原文字面量(若 `.qm` 没编译或没装)。这样保证现有
    `widget.text() == "中文"` 断言保持兼容。
    """
    install_translator(qapp)  # zh_CN 默认
    label = QLabel()
    # tr() 应当把字符串原样返(因为 .qm 没编译 / 没装)
    label.setText(label.tr("中文测试"))
    assert label.text() == "中文测试"
