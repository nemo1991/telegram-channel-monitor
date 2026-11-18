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
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QStyleOptionViewItem

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.core.dto import MessageDTO
from tgmonitor.ui.widgets.message_item_delegate import MessageItemDelegate
from tgmonitor.ui.widgets.message_view import MessageListModel


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


def test_paint_and_size_hint_share_same_doc(qapp, qloop, tmp_path):
    """G6-1:同 key 下 sizeHint 与 paint 拿到同一个 doc 实例。"""
    win = build_real_main_window(qloop, tmp_path)
    view = win.live_view
    view.append(_msg(text="hello " + "x" * 200))
    idx = view._model.index(0, 0)
    delegate: MessageItemDelegate = view._delegate
    opt = _option(view)

    seen: list[int] = []
    original = delegate._layout

    def spy(text, font, width, key):
        doc = original(text, font, width, key)
        seen.append(id(doc))
        return doc

    delegate._layout = spy  # type: ignore[method-assign]
    try:
        delegate.sizeHint(opt, idx)
        # paint 用真 QPainter 对着一张 pixmap,只关心它调 _layout 拿到同一 doc
        pixmap = QPixmap(400, 100)
        painter = QPainter(pixmap)
        try:
            delegate.paint(painter, opt, idx)
        finally:
            painter.end()
    finally:
        delegate._layout = original  # type: ignore[method-assign]

    assert len(seen) == 2  # sizeHint 一次 + paint 一次
    assert len(set(seen)) == 1  # 两次拿到同一个 doc 实例(核心契约)
    win.close()


def test_layout_key_includes_width_and_font(qapp, qloop, tmp_path):
    """G6-2:cache key 含 width + font,不同宽度/字体各自重算。"""
    win = build_real_main_window(qloop, tmp_path)
    view = win.live_view
    view.append(_msg())
    idx = view._model.index(0, 0)
    delegate = view._delegate

    delegate.sizeHint(_option(view, 400), idx)
    delegate.sizeHint(_option(view, 300), idx)
    keys = list(delegate._doc_cache.keys())
    widths = {k[2] for k in keys}
    assert widths == {400, 300}  # 宽度进 key
    assert all(isinstance(k[3], tuple) and len(k[3]) == 4 for k in keys)  # font_key 进 key
    win.close()


def test_font_change_recomputes_layout(qapp, qloop, tmp_path):
    """G6-2:字体变化 → 新 key → 新 doc,不会"画旧字体、量新字体"。"""
    from PySide6.QtGui import QFont

    win = build_real_main_window(qloop, tmp_path)
    view = win.live_view
    view.append(_msg())
    idx = view._model.index(0, 0)
    delegate = view._delegate

    opt = _option(view)
    delegate.sizeHint(opt, idx)
    before = len(delegate._doc_cache)

    bold = QFont(view.font())
    bold.setPointSize(bold.pointSize() + 4)
    opt_bold = QStyleOptionViewItem()
    opt_bold.rect = QRect(0, 0, 400, 0)
    opt_bold.font = bold
    delegate.sizeHint(opt_bold, idx)
    after = len(delegate._doc_cache)

    assert after == before + 1  # 字体不同 → 新 doc
    win.close()


def test_no_dto_third_path_uses_layout(qapp, qloop, tmp_path):
    """G6-2:无 DtoRole 的 model 直接给 FormattedRole 也走 _layout,不崩且进缓存。"""
    win = build_real_main_window(qloop, tmp_path)
    view = win.live_view
    delegate = view._delegate
    opt = _option(view)

    # 用一个假 index:只给 FormattedRole,不给 DtoRole
    class _FakeIndex:
        def data(self, role):
            if role == MessageListModel.FormattedRole:
                return "no dto fallback text"
            return None

    fake = _FakeIndex()
    size = delegate.sizeHint(opt, fake)  # type: ignore[arg-type]
    assert size.width() == 400
    assert len(delegate._doc_cache) == 1
    # key 的 cid/mid 用 text 哈希兜底,不是 (None, None) 共用
    key = next(iter(delegate._doc_cache))
    assert key[0] is None and isinstance(key[1], int)
    win.close()


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


def test_typography_sans_serif_and_gray_time(qapp):
    """FR-03 步骤二:去等宽 <pre>、字体无衬线、时间灰字 span。"""
    html = MessageItemDelegate._plain_to_html("⏱ 10:30:45  [新闻]  #7  👤 alice\n  正文")
    assert "<pre" not in html  # 去等宽
    assert "font-family:sans-serif" in html  # 无衬线(与最近活动一致)
    assert "white-space:pre-wrap" in html and "word-wrap:break-word" in html  # 折行护栏
    assert "<span style='color:#7a7d88;'>⏱ 10:30:45</span>" in html  # 灰字时间
    assert "正文" in html  # 正文未被吞
    # 非时间开头的文本不误加 span
    assert "color:#7a7d88" not in MessageItemDelegate._plain_to_html("普通正文")


def test_long_text_and_url_no_clip_no_scroll(qapp, qloop, tmp_path):
    """G6-4:长文本/无空格 URL/多行消息不裁切、无横向滚动。"""
    win = build_real_main_window(qloop, tmp_path)
    view = win.live_view
    long_url = "https://example.com/" + "a" * 500
    multi = "line one\nline two\n" + "x" * 300
    view.append(_msg(1, long_url))
    view.append(_msg(2, multi))
    delegate = view._delegate
    for r in range(view.count()):
        idx = view._model.index(r, 0)
        size = delegate.sizeHint(_option(view, 360), idx)
        # 高度按容器宽折行得出,不是 idealWidth 撑出的巨大值
        assert 0 < size.height() < 2000
        assert size.width() == 360
    # 视图本身横向滚动条仍关闭(视图层 2.0.2 语义)
    from PySide6.QtCore import Qt

    assert view.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    win.close()
