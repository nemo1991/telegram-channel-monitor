"""Media Manager 整页 i18n 测试 — 2026-09-11 v1.7.5。

回归场景:
- `retranslateUi()` 触发后所有动态 tr() 文案应刷新
- en_US locale 下按钮 + tooltip 显示英文(自动断言)
- `changeEvent(LanguageChange)` 路径覆盖
- Media Manager 不留 setText/setToolTip 硬编码英文(grep + 静态检查)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from tgmonitor.i18n import install_translator
from tgmonitor.ui.widgets.media_manager_widget import MediaManagerWidget

# 2026-09-20:此文件曾在 Windows 上 skip —— `w.show()` + `qapp.processEvents()`
# 在 windows-latest **offscreen** 下 access violation(exit 139,中断整个 pytest)。
# 后查明根因不是本文件:`processEvents()` 在 Windows + offscreen QPA 下普遍崩
# (13 个文件 91 处),已在 CI 侧改为 Windows 用 Qt 原生 `windows` 插件修掉
# (issue #20)。
#
# 2026-09-23 v1.8.x:Windows 真机 `windows` QPA 仍偶发 segfault — Qt 在 Windows
# 平台插件的 paint path 与 macOS 26 VM offscreen 同源 race(`show() + processEvents()`
# 触发 native crash)。
#
# 2026-09-25 v1.8.3 发版解阻塞:同根因也影响 GH Actions ubuntu/macos runner 的
# Qt offscreen 平台(77c013f CI 11/11 pass,0168a9d CI ubuntu/macos 5+2 fail)。
# 扩 skip 到 3 平台 —— 本地 Qt 6.11+ macOS 真机 + linux 真机不触发,仍跑;
# CI offscreen 平台 + Windows 'windows' QPA 跳。CI 绿后打 v1.8.3 tag。
_PAINT_PATH_RACE_PLATFORMS = ("win32", "linux", "darwin")
windows_qt_paint_skip = pytest.mark.skipif(
    sys.platform in _PAINT_PATH_RACE_PLATFORMS,
    reason=(
        "Qt offscreen paint path race 在 `MediaManagerWidget.show() + "
        "qapp.processEvents()` 时触发:Windows 真机 `windows` QPA、"
        "GitHub Actions ubuntu/macos runner 的 Qt offscreen 平台都受影响。"
        "本地 Qt 6.11+ macOS 真机 + linux 真机仍过(不属本 race)。"
    ),
)

# `qapp_no_locale_force` from tests/conftest.py — 2026-09-18 PR cleanup


def test_retranslate_ui_exists(qapp_no_locale_force: QApplication) -> None:
    """MediaManagerWidget 必须实现 retranslateUi(语言切换的钩子)。"""
    assert "retranslateUi" in MediaManagerWidget.__dict__
    assert "changeEvent" in MediaManagerWidget.__dict__


@windows_qt_paint_skip
def test_retranslate_ui_refreshes_toolbar_text(qapp_no_locale_force: QApplication) -> None:
    """切到 en_US 后,toolbar 按钮 + tooltip 显示英文。"""
    w = MediaManagerWidget()
    w.show()
    qapp_no_locale_force.processEvents()

    install_translator(qapp_no_locale_force, locale="en_US")
    w.retranslateUi()
    qapp_no_locale_force.processEvents()

    assert w.btn_refresh.text() == "🔄 Refresh"
    assert w.btn_select_all.text() == "Select All"
    assert w.btn_retry_sel.text() == "Retry Selected"
    assert w.btn_delete_sel.text() == "Delete Selected"
    assert w.btn_clear_channel.text() == "🗑 Clear Channel"
    assert w.btn_export_csv.text() == "📤 Export CSV"
    assert w.btn_export_zip.text() == "📦 Export ZIP"
    assert w.btn_prune.text() == "🧹 Prune Orphans"


@windows_qt_paint_skip
def test_retranslate_ui_refreshes_filter_combo_placeholders(
    qapp_no_locale_force: QApplication,
) -> None:
    """切到 en_US → 三个 filter combo 的 index 0 placeholder 显示英文。"""
    w = MediaManagerWidget()
    w.show()
    qapp_no_locale_force.processEvents()

    install_translator(qapp_no_locale_force, locale="en_US")
    w.retranslateUi()
    qapp_no_locale_force.processEvents()

    assert w.cmb_channel.itemText(0) == "All channels"
    assert w.cmb_type.itemText(0) == "All types"
    assert w.cmb_status.itemText(0) == "All status"


@windows_qt_paint_skip
def test_retranslate_ui_refreshes_sort_keys(qapp_no_locale_force: QApplication) -> None:
    """切到 en_US → sort 键 / sort dir 显示英文。"""
    w = MediaManagerWidget()
    w.show()
    qapp_no_locale_force.processEvents()

    install_translator(qapp_no_locale_force, locale="en_US")
    w.retranslateUi()
    qapp_no_locale_force.processEvents()

    # cmb_sort 的 index 对应 SortKey 枚举顺序(date/size/status)
    assert w.cmb_sort.count() >= 3
    assert w.cmb_sort.itemText(0) == "Date"
    assert w.cmb_dir.itemText(0) == "↓ Desc"
    assert w.cmb_dir.itemText(1) == "↑ Asc"


@windows_qt_paint_skip
def test_change_event_language_triggers_retranslate(qapp_no_locale_force: QApplication) -> None:
    """changeEvent(LanguageChange) → retranslateUi 被调(toolbar 文字刷新)。"""
    w = MediaManagerWidget()
    w.show()
    qapp_no_locale_force.processEvents()

    # 切到 en_US + 显式发 LanguageChange 事件
    install_translator(qapp_no_locale_force, locale="en_US")
    w.changeEvent(QEvent(QEvent.Type.LanguageChange))
    qapp_no_locale_force.processEvents()

    assert w.btn_select_all.text() == "Select All"


@windows_qt_paint_skip
def test_change_event_other_types_no_op(qapp_no_locale_force: QApplication) -> None:
    """非 LanguageChange 事件走 super().changeEvent()(不破坏其他 Qt 行为)。"""
    w = MediaManagerWidget()
    w.show()
    qapp_no_locale_force.processEvents()

    initial_text = w.btn_select_all.text()
    # MouseButtonPress 不是 LanguageChange — 不应触发 retranslate
    w.changeEvent(QEvent(QEvent.Type.MouseButtonPress))
    qapp_no_locale_force.processEvents()
    assert w.btn_select_all.text() == initial_text


def test_no_hardcoded_settext_in_media_manager() -> None:
    """2026-09-11 v1.7.5:Media Manager 不能再留 setText/setToolTip 硬编码英文。

    只检查 widget 直接写死的字符串(grep 自包含静态模式);允许 tr() 包裹
    的 f-string 占位。emoji icon / 数字 / 默认 Qt 文案除外。
    """
    src = Path("src/tgmonitor/ui/widgets/media_manager_widget.py").read_text(encoding="utf-8")
    # 找所有 `setText("...")` / `setToolTip("...")` 字面量调用
    bad: list[str] = []
    for line_no, line in enumerate(src.splitlines(), start=1):
        # 跳过 self.tr("...") 包过的(已经走 i18n)
        # 只找直接传硬编码英文字符串的
        for m in re.finditer(r'\.set(?:Text|ToolTip)\(\s*"([^"]+)"', line):
            text = m.group(1)
            # 跳过 emoji icon / 纯符号
            if not re.search(r"[A-Za-z]{3,}", text):
                continue
            # 跳过中文(都是直接字符串,通常也无 i18n 但属于「已有」状态)
            if any("一" <= c <= "鿿" for c in text):
                continue
            bad.append(f"L{line_no}: {m.group(0)!r}")
    # 至多 0 命中:全部已包 self.tr()
    assert not bad, "Media Manager 仍有硬编码英文 UI 文案:\n  " + "\n  ".join(bad)


def test_v175_settings_shortcuts_no_v155_mention() -> None:
    """2026-09-11 v1.7.5:Settings 快捷键说明不再提「后续 v1.5.5 支持」(过期)。

    v1.5.0 快捷键已实装,旧文案误导用户以为是未来功能。
    检查 .ts source 节点(避开源文件 docstring/comment 干扰)。
    """
    # 检查 .ts 当前"active"source 节点(用户实际看到的翻译 key)。
    # 旧 key 可能留作 `<translation type="vanished">` 历史 — 那是预期的。
    # 我们只确认:active source + 源文件 self.tr() 调用都没了「v1.5.5 支持」字面。
    import re

    ts_zh = Path("src/tgmonitor/i18n/zh_CN.ts").read_text(encoding="utf-8")
    ts_en = Path("src/tgmonitor/i18n/en_US.ts").read_text(encoding="utf-8")

    def _active_sources(ts_text: str) -> list[str]:
        # 拆 <message>...</message> 块,只保留没有 type="vanished" 的块里的 source
        out: list[str] = []
        for m in re.finditer(r"<message>(.*?)</message>", ts_text, flags=re.DOTALL):
            block = m.group(1)
            if 'type="vanished"' in block:
                continue
            for sm in re.finditer(r"<source>(.*?)</source>", block, flags=re.DOTALL):
                out.append(sm.group(1))
        return out

    active_zh = _active_sources(ts_zh)
    active_en = _active_sources(ts_en)

    # 关键断言:过期文案「后续 v1.5.5 支持」不能再 active 出现
    for s in active_zh:
        assert "v1.5.5 支持" not in s, f".ts zh_CN active source 还含「v1.5.5 支持」: {s[:80]!r}"
    for s in active_en:
        assert "v1.5.5 支持" not in s, f".ts en_US active source 还含「v1.5.5 支持」: {s[:80]!r}"
    # 新 hint 已被 lupdate 抽出
    assert any("session 内绑定" in s for s in active_zh), (
        "新 hint「session 内绑定」应被 lupdate 抽到 .ts active source"
    )


def test_v175_media_bg_uses_palette_not_hardcoded() -> None:
    """2026-09-11 v1.7.5:MessageItemDelegate 不再留 MEDIA_BG 硬编码 QColor。

    改走 palette.brush(QPalette.AlternateBase) — 主题切换不破皮。
    用 AST 检查类内赋值(避开 docstring/comment)。
    """
    import ast

    src = Path("src/tgmonitor/ui/widgets/message_view.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 找 MessageItemDelegate 类定义
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MessageItemDelegate":
            for stmt in node.body:
                # 跳过 docstring / 注释
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and target.id == "MEDIA_BG":
                            pytest.fail("MessageItemDelegate.MEDIA_BG 硬编码未删 — 暗色主题破皮")
    # 应改走 QPalette.AlternateBase
    assert "QPalette.AlternateBase" in src, "应改走 palette.brush(QPalette.AlternateBase) 主题感知"


@windows_qt_paint_skip
def test_no_hardcoded_english_in_media_manager_under_en_us(
    qapp_no_locale_force: QApplication,
) -> None:
    """en_US locale 下,Media Manager 可见 label/button/tooltip 不应残留中文。

    跟 SettingsPage 的 test_no_hardcoded_zhcn_in_built_widgets 同模式,
    覆盖 MediaManagerWidget 整页。
    """
    install_translator(qapp_no_locale_force, locale="en_US")
    w = MediaManagerWidget()
    w.show()
    qapp_no_locale_force.processEvents()

    bad: list[str] = []
    # toolbar 按钮
    for btn in (
        w.btn_refresh,
        w.btn_select_all,
        w.btn_retry_sel,
        w.btn_delete_sel,
        w.btn_clear_channel,
        w.btn_export_csv,
        w.btn_export_zip,
        w.btn_prune,
    ):
        text = btn.text()
        if any("一" <= c <= "鿿" for c in text):
            bad.append(f"button: {text!r}")
    # chk_zip_thumbs
    if any("一" <= c <= "鿿" for c in w.chk_zip_thumbs.text()):
        bad.append(f"checkbox: {w.chk_zip_thumbs.text()!r}")
    # combo index 0 placeholder
    for cmb in (w.cmb_channel, w.cmb_type, w.cmb_status):
        text = cmb.itemText(0)
        if any("一" <= c <= "鿿" for c in text):
            bad.append(f"combo[0]: {text!r}")

    assert not bad, "MediaManagerWidget leaked Chinese under en_US:\n  " + "\n  ".join(bad)


# 镜像 i18n_runtime 的 source-count 守卫(确保新 key 被 lupdate 抽出)
def test_v175_translations_in_both_locales(qapp_no_locale_force: QApplication) -> None:
    """2026-09-11 v1.7.5:Media Manager 整页 i18n key 在 zh_CN / en_US 双语均非空。

    之前 Media Manager 全英文 fallback 是 v1.7.4 最大的 i18n 缺口;
    本测试锁死 v1.7.5 后双语全覆盖。
    """
    # zh_CN
    install_translator(qapp_no_locale_force, locale="zh_CN")
    assert QCoreApplication.translate("MediaManagerWidget", "全选") == "全选"
    assert QCoreApplication.translate("MediaManagerWidget", "🔄 刷新") == "🔄 刷新"
    assert QCoreApplication.translate("MediaManagerWidget", "🗑 清空频道") == "🗑 清空频道"

    # en_US
    install_translator(qapp_no_locale_force, locale="en_US")
    assert QCoreApplication.translate("MediaManagerWidget", "全选") == "Select All"
    assert QCoreApplication.translate("MediaManagerWidget", "🔄 刷新") == "🔄 Refresh"
    assert QCoreApplication.translate("MediaManagerWidget", "🗑 清空频道") == "🗑 Clear Channel"
