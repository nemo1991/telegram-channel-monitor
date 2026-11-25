"""v2.0.5/v2.0.7 消息详情精修契约 — 去正文小标题 / 去关闭详情 / 复制按钮上提。

- 无正文小标题 QLabel;正文 v2.0.7 起是 QLabel(#detailTextEdit)且内容不变;
- 无「关闭详情」按钮;
- 复制文本按钮与 #msg_id 标头同处一行(header),点击仍写剪贴板 + emit。
- 其它小节标题(媒体 / 原始 JSON)不受影响。
"""

from __future__ import annotations

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLabel, QPushButton

from tests.fixtures._factories import make_message
from tgmonitor.ui.widgets.message_detail import MessageDetail


def _labels(detail: MessageDetail) -> list[QLabel]:
    return detail.findChildren(QLabel)


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
