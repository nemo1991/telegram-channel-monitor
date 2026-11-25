"""v2.0.5 FR-06 契约 — delegate 布局结构性重构。

- G6-1:paint 与 sizeHint 读同一 QTextDocument 实例(_layout 单一入口),
  从结构上消除"画的高度 = 报的行高"隐式契约。
- G6-2:四个漏改点从结构上消除 — 字体进 cache key、宽度统一口径、
  padding 由 documentMargin 表达(无 +8 魔数)、无 DTO 第三路径收编。
- G6-3:公开契约不变 — clear_caches / _DOC_CACHE_MAX / _plain_to_html /
  paint / sizeHint 及旧属性 _doc_cache / _size_hint_cache 仍可用。
- G6-4:长文本/URL/多行消息不裁切、无横向滚动(复用 2.0.4/2.0.1 护栏)。
- 注:perf 基线对比由 tests/perf/benchmark_message_view.py 手工跑,
  改造前数值登记在 PR 描述 / RK-06(本文件不写死绝对耗时,避免机差误报)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QStyleOptionViewItem

from tgmonitor.core.dto import MessageDTO
from tgmonitor.ui.widgets.message_item_delegate import MessageItemDelegate


def _msg(mid: int = 42, text: str = "hello") -> MessageDTO:
    return MessageDTO(
        id=0,
        channel_id=1,
        telegram_msg_id=mid,
        text=text,
        date=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _option(view, width: int = 400) -> QStyleOptionViewItem:
    opt = QStyleOptionViewItem()
    opt.rect = QRect(0, 0, width, 0)
    opt.font = view.font()
    return opt


def test_public_contract_preserved(qapp):
    """G6-3:公开契约不变 — 常量/方法/旧 cache 属性仍存在可用。"""
    assert MessageItemDelegate._DOC_CACHE_MAX == 200
    assert callable(MessageItemDelegate._plain_to_html)
    assert "white-space:pre-wrap" in MessageItemDelegate._plain_to_html("a\nb")
    d = MessageItemDelegate()
    assert hasattr(d, "_doc_cache") and hasattr(d, "_size_hint_cache")
    d.clear_caches()
    assert len(d._doc_cache) == 0 and len(d._size_hint_cache) == 0
    d.deleteLater()
