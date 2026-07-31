"""MessageDetail QSS 主题化:6 处 inline `setStyleSheet` 全部抽到全局 QSS 后,
每个 widget 仍能被 objectName 定位。本测试只断"objectName 设置成功"
— 不真跑 QSS 渲染(offscreen 平台样式表不一定完整应用)。

约定(2026-07-22):
  - `#detailHeader` — 顶部 #msg_id
  - `#detailSectionLabel` — 小节标题(📝 正文 / 📎 媒体 / 🔍 原始 JSON)
  - `#detailTextEdit` — 正文 QPlainTextEdit
  - `#mediaItem` — 媒体卡 QLabel
  - `#rawJsonEdit` — 原始 JSON QPlainTextEdit
  - `QLabel#emptyHintIcon` — 空状态占位 icon(form_row + message_detail 共用)
"""
from __future__ import annotations

from datetime import datetime

import pytest
from PySide6.QtWidgets import QApplication

from tgmonitor.core.dto import MediaDTO, MediaType, MessageDTO
from tgmonitor.ui.widgets.form_row import empty_hint
from tgmonitor.ui.widgets.message_detail import MessageDetail


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_msg() -> MessageDTO:
    return MessageDTO(
        id=0, channel_id=100, telegram_msg_id=42,
        text="hello 世界", author="alice",
        date=datetime(2026, 7, 15, 13, 50, 10),
        media=[MediaDTO(
            type=MediaType.PHOTO, mime_type="image/jpeg",
            file_size=1234, width=800, height=600,
        )],
        raw={"_": "raw"},
    )


# ---- objectName 是 detail QSS selector 命中前提 ----


def test_detail_header_has_objectname(qapp) -> None:
    """顶部 #msg_id label → objectName='detailHeader' 给 QSS selector。"""
    from PySide6.QtWidgets import QLabel
    detail = MessageDetail()
    detail.show_message(_make_msg())
    inner = detail.widget()  # the wrap QWidget
    headers = [
        lbl for lbl in inner.findChildren(QLabel)
        if lbl.objectName() == "detailHeader"
    ]
    assert len(headers) == 1, "detailHeader 应该被 setObjectName 标到"
    assert "#42" in headers[0].text()


def test_section_labels_have_objectname(qapp) -> None:
    """show_message 后应出现 3 个 detailSectionLabel(📝 正文 / 📎 媒体 / 🔍 原始 JSON)。"""
    from PySide6.QtWidgets import QLabel
    detail = MessageDetail()
    detail.show_message(_make_msg())
    inner = detail.widget()
    labels = [
        lbl for lbl in inner.findChildren(QLabel)
        if lbl.objectName() == "detailSectionLabel"
    ]
    titles = [lbl.text() for lbl in labels]
    assert "📝 正文" in titles
    assert any("📎 媒体" in t for t in titles)
    assert "🔍 原始 JSON" in titles


def test_text_edit_has_objectname(qapp) -> None:
    """正文 QPlainTextEdit 应带 objectName='detailTextEdit'。"""
    from PySide6.QtWidgets import QPlainTextEdit
    detail = MessageDetail()
    detail.show_message(_make_msg())
    inner = detail.widget()
    edits = inner.findChildren(QPlainTextEdit)
    # 2 个 QPlainTextEdit:正文 + 原始 JSON
    obj_names = sorted(e.objectName() for e in edits)
    assert obj_names == ["detailTextEdit", "rawJsonEdit"]


def test_media_item_label_has_objectname(qapp) -> None:
    """每条 media 渲染成一个 #mediaItem QLabel。"""
    from PySide6.QtWidgets import QLabel
    detail = MessageDetail()
    detail.show_message(_make_msg())
    inner = detail.widget()
    items = [
        lbl for lbl in inner.findChildren(QLabel)
        if lbl.objectName() == "mediaItem"
    ]
    assert len(items) == 1, "msg 里 1 条 media,应有 1 个 mediaItem"
    # 内容由 _format_media 生成
    assert "photo" in items[0].text()


def test_no_inlined_setstylesheet_on_detail_widgets(qapp) -> None:
    """回归保护:message_detail.py 不应再对任何 widget 调 `setStyleSheet(...)` 字符串字面量。

    直接读源码 — Qt 样式表赋值的字符串应在 QSS 文件中,不在 .py 里散落。
    `styleSheet()` 在本测试运行时返空(因为我们没 load QSS 资源),所以
    该属性读出来应该是空串。
    """
    detail = MessageDetail()
    detail.show_message(_make_msg())
    inner = detail.widget()
    # 任何内部 QLabel / QPlainTextEdit 都应 styleSheet() == ''
    leaked = []
    for child in inner.findChildren(type(inner)):
        if child.styleSheet():
            leaked.append((child.__class__.__name__, child.objectName(), child.styleSheet()))
    assert not leaked, (
        f"内联样式偷偷留在了 widget tree:{leaked[:3]}"
    )


def test_empty_hint_icon_has_objectname(qapp) -> None:
    """empty_hint helper 的 icon label 应带 objectName='emptyHintIcon'
    给 QSS,与 message_detail 占位 icon 共用同一 selector。
    """
    from PySide6.QtWidgets import QLabel
    panel = empty_hint("💬", "暂无消息", "hint")
    icons = [
        lbl for lbl in panel.findChildren(QLabel)
        if lbl.objectName() == "emptyHintIcon"
    ]
    assert len(icons) == 1
    assert icons[0].text() == "💬"


def test_message_detail_empty_state_has_empty_hint_icon(qapp) -> None:
    """show_message(None) 后占位 UI 的 icon 也带 #emptyHintIcon objectName —
    跟 form_row 走同一 QSS 规则。
    """
    from PySide6.QtWidgets import QLabel
    detail = MessageDetail()
    detail.show_message(None)
    inner = detail.widget()
    icons = [
        lbl for lbl in inner.findChildren(QLabel)
        if lbl.objectName() == "emptyHintIcon"
    ]
    assert len(icons) == 1
    assert icons[0].text() == "💬"


# ---- QSS 文件包含必要 selector(避免 silent break) ----


def test_light_qss_has_required_selectors() -> None:
    """style.qss 必须有 detailHeader / detailSectionLabel / mediaItem 等 selector,
    否则即使 objectName 设了样式也不生效。"""
    from pathlib import Path
    qss_path = Path(__file__).parent.parent / "src" / "tgmonitor" / "ui" / "resources" / "style.qss"
    text = qss_path.read_text(encoding="utf-8")
    for sel in (
        "#detailHeader",
        "#detailSectionLabel",
        "#detailTextEdit",
        "#mediaItem",
        "#rawJsonEdit",
        "QLabel#emptyHintIcon",
    ):
        assert sel in text, f"selector {sel!r} 缺失 in style.qss"


def test_dark_qss_has_required_selectors() -> None:
    """dark 主题也得有,否则切换主题后样式断。"""
    from pathlib import Path
    qss_path = Path(__file__).parent.parent / "src" / "tgmonitor" / "ui" / "resources" / "style_dark.qss"
    text = qss_path.read_text(encoding="utf-8")
    for sel in (
        "#detailHeader",
        "#detailSectionLabel",
        "#detailTextEdit",
        "#mediaItem",
        "#rawJsonEdit",
        "QLabel#emptyHintIcon",
    ):
        assert sel in text, f"selector {sel!r} 缺失 in style_dark.qss"
