"""v2.0.5 消息详情精修契约 — 去正文小标题 / 去关闭详情 / 复制按钮上提 header。

- 无正文小标题QLabel,正文 QPlainTextEdit(#detailTextEdit)仍在且内容不变;
- 无「关闭详情」按钮;
- 复制文本按钮与 #msg_id 标头同处一行(header),点击仍写剪贴板 + emit。
- 其它小节标题(媒体 / 原始 JSON)不受影响。
"""

from __future__ import annotations

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QPushButton

from tests.fixtures._factories import make_message
from tgmonitor.ui.widgets.message_detail import MessageDetail


def _labels(detail: MessageDetail) -> list[QLabel]:
    return detail.findChildren(QLabel)


def test_no_body_section_label_but_text_edit_present(qapp) -> None:
    """F1:去掉正文小标题小标题,但正文文本框仍在、内容不变。"""
    detail = MessageDetail()
    detail.show_message(make_message(msg_id=7, text="正文内容 ABC"))

    assert not any("正文" in lbl.text() for lbl in _labels(detail)), "不应再显示正文小标题"
    text_edit = detail.findChild(QPlainTextEdit, "detailTextEdit")
    assert text_edit is not None
    assert "正文内容 ABC" in text_edit.toPlainText()


def test_other_section_labels_intact(qapp) -> None:
    """F1 边界:媒体 / 原始 JSON 小节标题保留,未被误删。"""
    from tests.fixtures._factories import make_photo

    photo_msg = make_photo()
    detail = MessageDetail()
    detail.show_message(make_message(msg_id=8, text="x", media=photo_msg.media, raw={"a": 1}))
    texts = [lbl.text() for lbl in _labels(detail)]
    assert any("媒体" in t for t in texts)
    assert any("原始 JSON" in t for t in texts)


def test_close_button_removed(qapp) -> None:
    """F2:不再有「关闭详情」按钮。"""
    detail = MessageDetail()
    detail.show_message(make_message(msg_id=9, text="x"))

    labels = [b.text() for b in detail.findChildren(QPushButton)]
    assert not any("关闭详情" in t for t in labels)


def test_copy_button_shares_header_row_with_msgid(qapp) -> None:
    """F3:复制文本按钮与 #msg_id 在同一个 QHBoxLayout(header 行)。"""
    detail = MessageDetail()
    detail.show_message(make_message(msg_id=12, text="x"))

    header = detail.findChild(QLabel, "detailHeader")
    assert header is not None
    assert header.text() == "#12"

    copy_btn = next((b for b in detail.findChildren(QPushButton) if "复制文本" in b.text()), None)
    assert copy_btn is not None
    # 用几何断言"同一行平行":两者的垂直中心对齐,且按钮在 msg_id 右侧。
    # (Qt 中 addWidget 的子控件 parent 是 wrap,拿不到所在 QHBoxLayout 引用,
    #  故按可见几何而非布局归属断言。)
    qapp.processEvents()
    h_center = header.mapTo(header.parentWidget(), header.rect().center()).y()
    b_center = copy_btn.mapTo(copy_btn.parentWidget(), copy_btn.rect().center()).y()
    assert abs(h_center - b_center) <= 2, (
        f"复制按钮应与 #msg_id 垂直平行,实际 y 中心 {b_center} vs {h_center}"
    )
    h_x = header.mapTo(header.parentWidget(), header.rect().center()).x()
    b_x = copy_btn.mapTo(copy_btn.parentWidget(), copy_btn.rect().center()).x()
    assert b_x > h_x, "复制按钮应在 #msg_id 右侧(右上角)"


def test_copy_button_still_copies_text(qapp) -> None:
    """F3:上提位置后复制行为不变 —— 点击写剪贴板 + emit 文本。"""
    detail = MessageDetail()
    detail.show_message(make_message(msg_id=13, text="COPY ME"))

    copy_btn = next((b for b in detail.findChildren(QPushButton) if "复制文本" in b.text()), None)
    assert copy_btn is not None and copy_btn.isEnabled()

    got: list[str] = []
    detail.copy_text_requested.connect(got.append)
    copy_btn.click()
    assert QGuiApplication.clipboard().text() == "COPY ME"
    assert got == ["COPY ME"]


def test_copy_button_disabled_when_no_text(qapp) -> None:
    """F3 边界:空正文消息复制按钮灰掉(逻辑随上提一起保留)。"""
    detail = MessageDetail()
    detail.show_message(make_message(msg_id=14, text=""))

    copy_btn = next((b for b in detail.findChildren(QPushButton) if "复制文本" in b.text()), None)
    assert copy_btn is not None
    assert not copy_btn.isEnabled()
