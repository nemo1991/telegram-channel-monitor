"""2026-11-16 UI 改进：消息详情面板字段契约。

守护两条：
- 频道显示名称（由外部注入映射，无名称回退 #id），不再硬编码频道 id
- 移除「浏览」字段（只读看板里浏览数无实际意义）
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel

from tests.fixtures._factories import make_message
from tgmonitor.core.dto import ReactionDTO
from tgmonitor.ui.widgets.message_detail import MessageDetail


def _label_texts(detail: MessageDetail) -> list[str]:
    """当前详情面板里所有 QLabel 文本（含字段名与字段值）。"""
    return [lbl.text() for lbl in detail.findChildren(QLabel)]


def test_detail_renders_reactions_without_crash(qapp) -> None:
    """带 reactions 的消息打开详情必须正常渲染「反应」行，不抛 AttributeError。

    2026-11-19 现场崩溃：`show_message()` 曾误把模块级 `_format_reactions`
    当实例方法调用（`self._format_reactions`），只要消息带 reaction 就必崩。
    此前所有详情测试都不构造 reactions，导致该分支从未被执行。
    """
    detail = MessageDetail()
    detail.show_message(
        make_message(
            channel_id=100,
            msg_id=7,
            reactions=[
                ReactionDTO(emoji="🔥", count=5, is_chosen=True),
                ReactionDTO(emoji="👍", count=3),
            ],
        )
    )

    texts = _label_texts(detail)
    assert "反应:" in texts, f"应显示反应字段，实际 labels={texts}"
    assert "[🔥 5]  👍 3" in texts, f"应显示 reaction 文本（自投高亮），实际 labels={texts}"
