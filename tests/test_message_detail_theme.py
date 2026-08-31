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
from PySide6.QtCore import Qt
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
        id=0,
        channel_id=100,
        telegram_msg_id=42,
        text="hello 世界",
        author="alice",
        date=datetime(2026, 7, 15, 13, 50, 10),
        media=[
            MediaDTO(
                type=MediaType.PHOTO,
                mime_type="image/jpeg",
                file_size=1234,
                width=800,
                height=600,
            )
        ],
        raw={"_": "raw"},
    )


# ---- objectName 是 detail QSS selector 命中前提 ----


def test_detail_header_has_objectname(qapp) -> None:
    """顶部 #msg_id label → objectName='detailHeader' 给 QSS selector。"""
    from PySide6.QtWidgets import QLabel

    detail = MessageDetail()
    detail.show_message(_make_msg())
    inner = detail.widget()  # the wrap QWidget
    headers = [lbl for lbl in inner.findChildren(QLabel) if lbl.objectName() == "detailHeader"]
    assert len(headers) == 1, "detailHeader 应该被 setObjectName 标到"
    assert "#42" in headers[0].text()


def test_section_labels_have_objectname(qapp) -> None:
    """show_message 后应出现 3 个 detailSectionLabel(📝 正文 / 📎 媒体 / 🔍 原始 JSON)。"""
    from PySide6.QtWidgets import QLabel

    detail = MessageDetail()
    detail.show_message(_make_msg())
    inner = detail.widget()
    labels = [lbl for lbl in inner.findChildren(QLabel) if lbl.objectName() == "detailSectionLabel"]
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
    items = [lbl for lbl in inner.findChildren(QLabel) if lbl.objectName() == "mediaItem"]
    assert len(items) == 1, "msg 里 1 条 media,应有 1 个 mediaItem"
    # 内容由 _format_media 生成
    assert "photo" in items[0].text()


def test_previewable_media_label_emits_signal_on_click(qapp) -> None:
    """2026-08-31 v1.5.0 PR #A8:Lightbox — 图片类(PHOTO)且 DONE 状态
    的媒体卡点击 → emit preview_requested(channel_id, msg_id, idx)。

    非图(PHOTO 之外 type)或未下载(PENDING)的媒体卡不响应 — 走 fallback
    路径(系统查看器 / 下载队列)。
    """
    from PySide6.QtCore import QEvent, QPoint
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QLabel

    from tgmonitor.core.dto import MediaDownloadStatus, MediaType

    done_photo = MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name="a.jpg",
        file_size=2048,
        telegram_file_id="fid-photo",
        object_key="media/a.jpg",
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )
    pending_video = MediaDTO(
        type=MediaType.VIDEO,
        mime_type="video/mp4",
        file_name="b.mp4",
        file_size=10_000_000,
        telegram_file_id="fid-video",
        download_status=MediaDownloadStatus.PENDING,
    )
    msg = MessageDTO(
        id=0,
        channel_id=77,
        telegram_msg_id=99,
        text="two-media",
        media=[done_photo, pending_video],
        raw={"_": "raw"},
    )
    detail = MessageDetail()
    detail.show_message(msg)
    inner = detail.widget()
    items = [lbl for lbl in inner.findChildren(QLabel) if lbl.objectName() == "mediaItem"]
    assert len(items) == 2

    # 接信号 → 触发后 captured 收 (channel_id, msg_id, idx)
    captured: list[tuple[int, int, int]] = []
    detail.preview_requested.connect(lambda c, m, i: captured.append((c, m, i)))

    photo_label, video_label = items[0], items[1]

    # ---- 1. DONE PHOTO → 点击应 emit preview_requested(77, 99, 0) ----
    assert photo_label.toolTip() == "点击查看大图"
    click_photo = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPoint(5, 5),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    photo_label.mousePressEvent(click_photo)
    assert captured == [(77, 99, 0)], f"PHOTO/DONE 点击未触发 lightbox 信号:captured={captured}"

    # ---- 2. PENDING VIDEO → 点击不应 emit(走系统查看器 fallback 路径)----
    click_video = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPoint(5, 5),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    video_label.mousePressEvent(click_video)
    assert captured == [(77, 99, 0)], f"PENDING VIDEO 被错误标为可点:captured={captured}"


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
    assert not leaked, f"内联样式偷偷留在了 widget tree:{leaked[:3]}"


def test_empty_hint_icon_has_objectname(qapp) -> None:
    """empty_hint helper 的 icon label 应带 objectName='emptyHintIcon'
    给 QSS,与 message_detail 占位 icon 共用同一 selector。
    """
    from PySide6.QtWidgets import QLabel

    panel = empty_hint("💬", "暂无消息", "hint")
    icons = [lbl for lbl in panel.findChildren(QLabel) if lbl.objectName() == "emptyHintIcon"]
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
    icons = [lbl for lbl in inner.findChildren(QLabel) if lbl.objectName() == "emptyHintIcon"]
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

    qss_path = (
        Path(__file__).parent.parent / "src" / "tgmonitor" / "ui" / "resources" / "style_dark.qss"
    )
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
