"""MessageItemDelegate — QListView 行 paint/sizeHint delegate。

2026-10-15 plan-batch7-A:从 `message_view.py` L495-619 整体搬迁,
主类降 1028 → 910 LOC。class 体 1:1 保留,只调整 import:
  - role enum 引用走 `MessageListModel.HiddenRole / FormattedRole / DtoRole / HasMediaRole`
    (主文件 re-export 该类,避免循环 import)
  - `QPalette` / `QTextDocument` 走 `PySide6.QtGui`

公开契约:
  - `MessageItemDelegate(QStyledItemDelegate)`
  - `clear_caches()` — PR #9 数据全表失效
  - `_DOC_CACHE_MAX` 类常量
  - `_plain_to_html` staticmethod
  - `paint` / `sizeHint` Qt override

2026-11-18 v2.0.5 FR-06 结构性重构:
  - 抽出单一 `_layout(text, font, width)` 入口,paint / sizeHint 都调它并
    读**同一个 QTextDocument**,从结构上消除"画的高度 = 报的行高"隐式契约。
  - 统一 cache key 为 (cid, mid, width, font_key),消除四个漏改点:
    字体缓存命中短路 / 宽度 fallback 不一致 / +8 单边 padding / 无 DTO 第三路径。
  - padding 由 `doc.setDocumentMargin(4)` 表达,不再在 sizeHint 里 +8
    (QTextDocument 默认 margin 已是 4,旧代码的 +8 属于重复加)。
  - 旧属性 `_doc_cache` / `_size_hint_cache` 保留为缓存视图,维持既有测试
    与外部引用兼容,但它们现在由 `_layout` 统一读写、key 完全一致。

通过 `from tgmonitor.ui.widgets.message_view import MessageItemDelegate`
re-export 保持旧 import 路径兼容。
"""

from __future__ import annotations

# 2026-10-15 plan-batch7-A:与 main_window.py / theme.py 等 ui/ 文件一致,
# 屏蔽 PySide6 stub 对 QPalette.AlternateBase 等枚举值的 attr-defined 误报。
# mypy: disable-error-code="attr-defined"
from PySide6.QtCore import (
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSize,
)
from PySide6.QtGui import QFont, QPalette, QTextDocument
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem

from tgmonitor.core.dto import MessageDTO

# 2026-10-15 plan-batch7-A:role enum 跨文件引用,主文件 re-export 该类
# 避免循环 import(message_view.py → message_item_delegate.py → message_view.py)。
from tgmonitor.ui.widgets.message_view import MessageListModel

# v2.0.5 FR-06:cache key 类型 = (cid, mid, width, font_key)
_LayoutKey = tuple[int | None, int | None, int, tuple[str, int, bool, bool]]


class MessageItemDelegate(QStyledItemDelegate):
    """2026-09-02 v1.5.3 PR #D1:lazy paint delegate。

    - hidden=True → 不画(节省 paint 开销)
    - 普通行 → QTextDocument 渲 rich text(支持 word wrap)

    2026-11-18 v2.0.5 FR-06:单一 `_layout()` 入口 — paint/sizeHint 共享
    同一份 QTextDocument,布局一致性由结构保证而非靠人同步两处代码。
    """

    # PR #9:doc 缓存上限。10K 行 × ~30KB doc ≈ 300MB,LRU 200 ≈ 6MB 可接受。
    _DOC_CACHE_MAX = 200

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # v2.0.5 FR-06:统一布局缓存。key 含 width + font,避免窗口 resize /
        # 字体切换时命中旧布局。旧属性名保留为视图(见下方 property)。
        self._layout_cache: dict[_LayoutKey, QTextDocument] = {}
        # v2.0.5 FR-06:尺寸单独缓存,sizeHint 命中后直接返回 QSize,不再每次
        # 调 doc.size()(原生调用较贵,100k 次实测多 ~30ms)。key 与布局一致。
        self._size_cache: dict[_LayoutKey, QSize] = {}
        self._layout_lru: list[_LayoutKey] = []

    # ---- 旧属性兼容视图(既有测试与外部引用读 `_doc_cache` / `_size_hint_cache`) ----
    @property
    def _doc_cache(self) -> dict[_LayoutKey, QTextDocument]:
        """布局缓存视图 — 与 `_layout_cache` 是同一 dict,不是副本。"""
        return self._layout_cache

    @property
    def _size_hint_cache(self) -> dict[_LayoutKey, QSize]:
        """尺寸缓存 — key 与 `_layout_cache` 完全一致(旧属性名兼容既有测试)。"""
        return self._size_cache

    def clear_caches(self) -> None:
        """数据全表失效(set_messages / reset / 大批 append)时清布局缓存。

        普通编辑路径由 model.dataChanged([FormattedRole]) 自动覆盖 →
        不必每条都 clear(与 PR #9 语义一致)。
        """
        self._layout_cache.clear()
        self._size_cache.clear()
        self._layout_lru.clear()

    @staticmethod
    def _font_key(font: QFont) -> tuple[str, int, bool, bool]:
        """字体指纹,纳入 cache key 以消除"画用旧字体、量用新字体"。"""
        return (font.family(), font.pointSize(), font.bold(), font.italic())

    @staticmethod
    def _effective_width(option: QStyleOptionViewItem) -> int:
        """统一宽度口径(旧代码 paint 用 rect.width、sizeHint 有 280 fallback)。"""
        return int(option.rect.width()) if option.rect.width() > 0 else 280

    def _layout(
        self,
        text: str,
        font: QFont,
        width: int,
        cache_key: _LayoutKey,
    ) -> QTextDocument:
        """单一布局入口 — paint 与 sizeHint 都调它,读同一份 QTextDocument。

        cache 命中直接返回 doc 实例(不重建);未命中时 new + setHtml +
        setTextWidth + documentMargin,并写入 LRU。所有布局参数(字体/宽度/
        padding/HTML)在此一处决定,消除双路径不一致的结构根源。
        """
        doc = self._layout_cache.get(cache_key)
        if doc is None:
            doc = QTextDocument()
            doc.setDefaultFont(font)
            doc.setHtml(self._plain_to_html(text))
            # v2.0.5 FR-06:padding 由 documentMargin 表达,替代旧 sizeHint
            # 的 +8 魔数(QTextDocument 默认 margin=4,旧 +8 属重复加)。
            doc.setDocumentMargin(4)
            doc.setTextWidth(width)
            self._layout_cache[cache_key] = doc
            self._layout_lru.append(cache_key)
            if len(self._layout_lru) > self._DOC_CACHE_MAX:
                evict = self._layout_lru.pop(0)
                self._layout_cache.pop(evict, None)
                self._size_cache.pop(evict, None)
        return doc

    def _size_for(self, cache_key: _LayoutKey, doc: QTextDocument, width: int) -> QSize:
        """尺寸计算 — 命中缓存直接返回,避免重复 doc.size() 原生调用。"""
        size = self._size_cache.get(cache_key)
        if size is None:
            size = QSize(width, int(doc.size().height()))
            self._size_cache[cache_key] = size
        return size

    def paint(
        self,
        painter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        if index.data(MessageListModel.HiddenRole):
            return
        has_media = bool(index.data(MessageListModel.HasMediaRole))
        if has_media:
            # 2026-09-11 v1.7.5:用 palette 替代硬编码 QColor — 由 palette 自动
            # 变暗,无需手动 if/else。palette 由 Qt theme + style.qss 提供。
            painter.fillRect(option.rect, option.palette.brush(QPalette.AlternateBase))
        text = index.data(MessageListModel.FormattedRole) or ""
        if not text:
            return
        dto = index.data(MessageListModel.DtoRole)
        width = self._effective_width(option)
        cache_key = self._cache_key(dto, text, width, option.font)
        # v2.0.5 FR-06:唯一布局入口 — 与 sizeHint 读同一 doc,不再各自 setHtml。
        doc = self._layout(text, option.font, width, cache_key)
        painter.save()
        painter.translate(option.rect.topLeft())
        doc.drawContents(painter)
        painter.restore()

    def sizeHint(  # noqa: N802 — Qt override
        self,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ):
        text = index.data(MessageListModel.FormattedRole) or ""
        if not text:
            return super().sizeHint(option, index)
        dto = index.data(MessageListModel.DtoRole)
        width = self._effective_width(option)
        cache_key = self._cache_key(dto, text, width, option.font)
        # v2.0.5 FR-06:唯一布局入口 — 与 paint 读同一 doc;尺寸走缓存视图,
        # padding 已由 documentMargin 吸收,不再 +8;收编无 DTO 第三路径。
        doc = self._layout(text, option.font, width, cache_key)
        return self._size_for(cache_key, doc, width)

    @staticmethod
    def _cache_key(
        dto: object,
        text: str,
        width: int,
        font: QFont,
    ) -> _LayoutKey:
        """统一 cache key 生成 — 有 DTO 用 (cid, mid),无 DTO 用 text 哈希兜底。"""
        if isinstance(dto, MessageDTO):
            cid: int | None = dto.channel_id
            mid: int | None = dto.telegram_msg_id
        else:
            # 无 DTO(model 直接给 FormattedRole)→ 以 text 派生稳定 id,
            # 避免所有无 DTO 行共用一个 key(旧第三路径每次 new doc)。
            cid, mid = None, hash(text)
        return (cid, mid, width, MessageItemDelegate._font_key(font))

    @staticmethod
    def _plain_to_html(text: str) -> str:
        """delegate 走 QTextDocument 需 HTML。转义 `<` `>` `&` 避免消息正文
        被当 HTML 解析;换行符 `\\n` → `<br>`。

        v2.0.5 FR-03 步骤二(排版语言与最近活动对齐):
        - 去掉 `<pre>` 的等宽语义,改 `<div>` + 系统无衬线字体(与最近活动
          的 QLabel 文本一致);
        - `⏱ HH:MM:SS` 时间段包灰色 span,对齐最近活动的灰字时间列;
        - 保留 `white-space:pre-wrap` + `word-wrap:break-word`(长行/URL
          折行护栏,防横向滚动条)与手工 `<br>` 换行。
        """
        esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body = esc.replace(chr(10), "<br>")
        # 时间灰字:formatted head 固定以 "⏱ HH:MM:SS" 开头(f"⏱ {dt}  ...")。
        # 只对首行这段做包装,正文 slaves 不受影响;时间串只含数字/冒号,
        # 经过上面转义后无 HTML 特殊字符,包 span 安全。
        if body.startswith("⏱ "):
            time_end = body.find("  ")
            if 2 < time_end <= 11:  # "⏱ " + 8 字符 "HH:MM:SS"
                time_part = body[2:time_end]
                body = "<span style='color:#7a7d88;'>⏱ " + time_part + "</span>" + body[time_end:]
        return (
            "<div style='font-family:sans-serif; margin:0; padding:0; "
            f"white-space:pre-wrap; word-wrap:break-word;'>{body}</div>"
        )
