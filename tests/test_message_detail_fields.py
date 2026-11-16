"""2026-11-16 UI 改进：消息详情面板字段契约。

守护两条：
- 频道显示名称（由外部注入映射，无名称回退 #id），不再硬编码频道 id
- 移除「浏览」字段（只读看板里浏览数无实际意义）
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel

from tests.fixtures._factories import make_message
from tgmonitor.ui.widgets.message_detail import MessageDetail


def _label_texts(detail: MessageDetail) -> list[str]:
    """当前详情面板里所有 QLabel 文本（含字段名与字段值）。"""
    return [lbl.text() for lbl in detail.findChildren(QLabel)]


def test_detail_shows_channel_title_not_id(qapp) -> None:
    """注入频道名映射后，详情「频道」行显示名称而非 #id。"""
    detail = MessageDetail()
    detail.set_channel_titles({100: "Tech News"})
    detail.show_message(make_message(channel_id=100, msg_id=5))

    texts = _label_texts(detail)
    assert "频道:" in texts
    assert "[Tech News]" in texts, f"详情应显示频道名，实际 labels={texts}"
    assert "#100" not in texts, f"详情不应残留硬编码频道 id，实际 labels={texts}"


def test_detail_falls_back_to_channel_id(qapp) -> None:
    """未注入名称的频道回退显示 [#id]，不抛异常也不留空。"""
    detail = MessageDetail()
    detail.show_message(make_message(channel_id=999, msg_id=5))

    texts = _label_texts(detail)
    assert "[#999]" in texts, f"应回退到 #id，实际 labels={texts}"


def test_detail_hides_views_field(qapp) -> None:
    """即使 DTO 带 views，详情面板也不再显示「浏览」字段。"""
    detail = MessageDetail()
    detail.show_message(make_message(channel_id=100, msg_id=5, views=123456))

    texts = _label_texts(detail)
    assert not any("浏览" in t for t in texts), f"详情不应显示浏览字段，实际 labels={texts}"
    # 其它元数据（如转发）不受影响（换 msg_id 触发 rebuild）
    detail.show_message(make_message(channel_id=100, msg_id=6, views=123456, forwards=42))
    texts = _label_texts(detail)
    assert "42" in texts, f"转发字段应保留，实际 labels={texts}"
