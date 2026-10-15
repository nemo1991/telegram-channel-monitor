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
from PySide6.QtGui import QPalette, QTextDocument
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem

from tgmonitor.core.dto import MessageDTO

# 2026-10-15 plan-batch7-A:role enum 跨文件引用,主文件 re-export 该类
# 避免循环 import(message_view.py → message_item_delegate.py → message_view.py)。
from tgmonitor.ui.widgets.message_view import MessageListModel


class MessageItemDelegate(QStyledItemDelegate):
    """2026-09-02 v1.5.3 PR #D1:lazy paint delegate。

    - hidden=True → 不画(节省 paint 开销)
    - media 行 → fillRect 底色(走 palette AlternateBase,主题切换不破皮)
    - 普通行 → QTextDocument 渲 rich text(支持 word wrap)

    2026-09-11 v1.7.5:删 `MEDIA_BG = QColor(232, 240, 248)` 硬编码浅蓝
    → 改走 `option.palette.brush(QPalette.AlternateBase)`(Qt 主题感知,
    暗色主题下自动给 darker shade)。同时支持 `style.qss` `#messageMediaRow`
    selector 自定义(若 QSS 设置 QPalette,override 优先级 QtStyle 决定)。

    2026-09-15 v1.7.5 PR #9 perf:加 `_size_hint_cache` + `_doc_cache` —
    - `_size_hint_cache: dict[(cid, mid, width), QSize]` — sizeHint O(1) 命中,
      避免每帧 QTextDocument 重建(setHtml + setTextWidth + size + idealWidth)
      单 row paint ~5-10ms → 缓存命中 ~0.1ms。
    - `_doc_cache: dict[(cid, mid), QTextDocument]` — paint 命中复用 doc 实例,
      避免 setHtml 重做。LRU 200(队列里 FIFO 淘汰,贴近真实窗口可见行 30-50,
      留 4x 缓冲)。dataChanged 时由调用方主动 clear(详见 `clear_caches`)。
    """

    # PR #9:doc 缓存上限。10K 行 × ~30KB doc ≈ 300MB,LRU 200 ≈ 6MB 可接受。
    _DOC_CACHE_MAX = 200

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # PR #9 perf:sizeHint 缓存 — keyed by (cid, mid, width)。width
        # 变化(窗口 resize)时本 row 自然 miss → 重算 + 缓存。
        self._size_hint_cache: dict[tuple[int, int, int], QSize] = {}
        # PR #9 perf:paint doc 缓存 — keyed by (cid, mid),LRU 200 淘汰。
        # doc 实例在 `paint` 内 reuse,只调 `setTextWidth(rect.width())`
        # 适配当前行宽 — 比重建省一次 setHtml。
        self._doc_cache: dict[tuple[int, int], QTextDocument] = {}
        self._doc_cache_lru: list[tuple[int, int]] = []  # FIFO 队列

    def clear_caches(self) -> None:
        """2026-09-15 v1.7.5 PR #9:数据全表失效(set_messages / reset /
        set_channel_titles / 大批 append)时清两个 cache。普通编辑路径由
        model.dataChanged([FormattedRole]) 自动覆盖 → 不必每条都 clear。
        """
        self._size_hint_cache.clear()
        self._doc_cache.clear()
        self._doc_cache_lru.clear()

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
            # 2026-09-11 v1.7.5:用 palette 替代硬编码 QColor — 暗色主题下自动
            # 变暗,无需手动 if/else。palette 由 Qt theme + style.qss 提供。
            painter.fillRect(option.rect, option.palette.brush(QPalette.AlternateBase))
        text = index.data(MessageListModel.FormattedRole) or ""
        if not text:
            return
        # 2026-09-15 v1.7.5 PR #9 perf:复用缓存 QTextDocument — 命中时省
        # setHtml 一遍(对长消息 ~1-3ms);未命中时新建 + 缓存 + 写 LRU。
        dto = index.data(MessageListModel.DtoRole)
        key = (dto.channel_id, dto.telegram_msg_id) if isinstance(dto, MessageDTO) else None
        doc: QTextDocument | None = None
        if key is not None and key in self._doc_cache:
            doc = self._doc_cache[key]
        else:
            doc = QTextDocument()
            doc.setDefaultFont(option.font)
            doc.setHtml(self._plain_to_html(text))
            if key is not None:
                self._doc_cache[key] = doc
                self._doc_cache_lru.append(key)
                if len(self._doc_cache_lru) > self._DOC_CACHE_MAX:
                    evict = self._doc_cache_lru.pop(0)
                    self._doc_cache.pop(evict, None)
        painter.save()
        painter.translate(option.rect.topLeft())
        doc.setTextWidth(option.rect.width())
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
        # 2026-09-15 v1.7.5 PR #9 perf:sizeHint 缓存。key = (cid, mid, width)。
        # width 变化 → miss → 重算并覆盖缓存(width 是 cache key 的一部分)。
        dto = index.data(MessageListModel.DtoRole)
        if isinstance(dto, MessageDTO):
            width = option.rect.width() if option.rect.width() > 0 else 280
            cache_key = (dto.channel_id, dto.telegram_msg_id, int(width))
            cached = self._size_hint_cache.get(cache_key)
            if cached is not None:
                return cached
            doc = QTextDocument()
            doc.setDefaultFont(option.font)
            doc.setHtml(self._plain_to_html(text))
            doc.setTextWidth(width)
            size = QSize(int(doc.idealWidth()), int(doc.size().height()) + 8)
            self._size_hint_cache[cache_key] = size
            return size
        # 没 DTO(model 直接给 FormattedRole)→ 走老路径
        doc = QTextDocument()
        doc.setDefaultFont(option.font)
        doc.setHtml(self._plain_to_html(text))
        width = option.rect.width() if option.rect.width() > 0 else 280
        doc.setTextWidth(width)
        # 高度 = 内容 + 上下各 4px padding
        return QSize(int(doc.idealWidth()), int(doc.size().height()) + 8)

    @staticmethod
    def _plain_to_html(text: str) -> str:
        """QListWidget.setText 走 plain text;delegate 走 QTextDocument
        需 HTML。换行符 `\n` → `<br>`,`&` `<` `>` 转义避免被当 HTML 解析。

        旧实现 `QListWidgetItem.text` 自动处理换行 + escape;这里需要手动。
        """
        esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<pre style='margin:0; padding:0;'>{esc.replace(chr(10), '<br>')}</pre>"
