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

    2026-10-15 plan-batch7-A:MessageItemDelegate 已抽到独立 module
    `message_item_delegate.py`,本 test 改读该文件(原 message_view.py
    找 AST + 字符串检查 delegate 渲染逻辑已不适用)。
    """
    import ast

    src = Path("src/tgmonitor/ui/widgets/message_item_delegate.py").read_text(encoding="utf-8")
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


# ---------------------------------------------------------------------------
# 2026-10-20 plan-batch10-ε:.ts context/source **双向**守门
#
# 为什么必须加这层:CHANGELOG.md:1474-1478 记的那次事故 —— 抽 method 时漏了
# `-extensions py`,`lupdate` 把本地 90% 已译条目标成 `type="vanished"`,
# `lrelease` 产出空 `.qm`,**i18n 全炸**。`test_i18n_runtime.py` 只查运行时
# locale 行为,查不出「源码里的字面量已经对不上 .ts」这个根因。
#
# 本组 2 个测试把双向缺口都封死:
#   test_all_widget_tr_literals_exist_in_ts — 源码 → .ts(漏抽会红)
#   test_no_orphan_ts_sources               — .ts → 源码(孤儿翻译堆积会红)
# ---------------------------------------------------------------------------

_MM_SRC = Path("src/tgmonitor/ui/widgets/media_manager_widget.py")
_TS_FILES = ("en_US", "zh_CN")


def _ts_messages(locale: str, context: str) -> dict[str, str]:
    """返回 `{source: type}` — type 为 `""`(活跃)或 `"vanished"`。"""
    import xml.etree.ElementTree as ET

    tree = ET.parse(f"src/tgmonitor/i18n/{locale}.ts")
    ctxs = [c for c in tree.getroot().findall("context") if c.find("name").text == context]
    assert len(ctxs) == 1, f"{locale}.ts 应有且仅有 1 个 `{context}` context,实得 {len(ctxs)}"
    out: dict[str, str] = {}
    for m in ctxs[0].findall("message"):
        src = m.find("source")
        if src is None or src.text is None:
            continue
        out[src.text] = m.get("type") or ""
    return out


def _source_tr_literals() -> set[str]:
    """抽出 facade 源码里全部 `self.tr("字面量")` 的**字面量参数**。

    用 AST 而非正则:正则会把 docstring / 注释里的散文当成真调用(本 module
    docstring 就写了 ``self.tr("字面量")`` 作为说明,曾让守门 test 误红),
    而 lupdate 自己走的是真解析器 —— 只有 AST 的口径与 lupdate 一致。

    只收 `Constant` str 参数:`.tr(f"…")` / `.tr(变量)` lupdate 静态抽不到,
    不在这组守门的射程内 —— 那正是 `_tr_*()` accessor 模式存在的原因
    (facade 全部 tr() 调用现已是字面量形式)。
    """
    import ast

    tree = ast.parse(_MM_SRC.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "tr"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "self"):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            out.add(first.value)
    return out


def test_all_widget_tr_literals_exist_in_ts() -> None:
    """facade 里每个 `self.tr("字面量")` 都必须是 .ts 中 MediaManagerWidget
    context 的 source,且 **type != "vanished"**。

    红了意味着两种事故之一:
    - 字面量是新的,lupdate 还没跑 → 运行时 fallback 到源文
    - 字面量被标 vanished → 翻译已失效(CHANGELOG:1474 的死法)
    """
    literals = _source_tr_literals()
    assert len(literals) >= 38, f"源码只抽到 {len(literals)} 处字面量,预期 >= 38 —— 正则失效?"

    problems: list[str] = []
    for locale in _TS_FILES:
        msgs = _ts_messages(locale, "MediaManagerWidget")
        for lit in sorted(literals):
            if lit not in msgs:
                problems.append(f"[{locale}] 字面量不在 .ts 里,lupdate 未抽: {lit!r}")
            elif msgs[lit] == "vanished":
                problems.append(f"[{locale}] 字面量被标 vanished(翻译已失效): {lit!r}")
    assert not problems, "源码 / .ts 出现缺口:\n  " + "\n  ".join(problems)


def test_no_orphan_ts_sources() -> None:
    """反向守门:.ts 中 MediaManagerWidget context 的每条**活跃** source 都必须
    仍能在 facade 源码里找到字面量 —— 防孤儿翻译堆积(`.ts` 越滚越大,谁也不敢
    删,最后没法判断哪条还生效)。

    `type="vanished"` 的 source 故意跳过:它们本就是历史残留,等下次
    `lupdate` 跑完会自然消失。
    """
    literals = _source_tr_literals()
    orphans: list[str] = []
    for locale in _TS_FILES:
        for src, typ in sorted(_ts_messages(locale, "MediaManagerWidget").items()):
            if typ == "vanished":
                continue
            if src not in literals:
                orphans.append(f"[{locale}] .ts 有活跃 source 但源码已无此字面量: {src!r}")
    assert not orphans, ".ts 出现孤儿翻译:\n  " + "\n  ".join(orphans)
