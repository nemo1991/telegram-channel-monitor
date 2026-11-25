"""v2.0.7 FR-03 契约 — 消息详情正文完整展示,区域内部不再出现滚动条。

- 正文是 QLabel(wordWrap) 自撑高,QPlainTextEdit 的 220px 上限已移除;
- 长文本由外层 MessageDetail(QScrollArea) 统一承接,正文自身无滚动能力;
- TextPlain 防止 Telegram 消息中的 < / & 被当 HTML;鼠标选中复制保留;
- 原始 JSON 的调试区域不动(仍是可滚动的 QPlainTextEdit)。
"""

from __future__ import annotations

from PySide6.QtWidgets import QAbstractScrollArea, QLabel, QPlainTextEdit

from tests.fixtures._factories import make_message
from tgmonitor.ui.widgets.message_detail import MessageDetail


def _body(detail: MessageDetail) -> QLabel:
    label = detail.findChild(QLabel, "detailTextEdit")
    assert label is not None
    return label


def test_body_is_autogrowing_label_not_scroll_area(qapp) -> None:
    detail = MessageDetail()
    detail.show_message(make_message(text="正文内容 ABC"))
    body = _body(detail)

    assert isinstance(body, QLabel)
    assert not isinstance(body, QAbstractScrollArea), "正文区域内不应再有滚动条"
    assert body.wordWrap()
    assert detail.findChild(QPlainTextEdit, "detailTextEdit") is None
    detail.deleteLater()
