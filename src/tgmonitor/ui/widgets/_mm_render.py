# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""RenderMixin — 行渲染管线 — 2026-10-20 plan-batch10-δ。

`MediaManagerWidget` 拆 3 mixin + facade 的**最后一步**:抽渲染域(最大块)。

- 16 method:`set_view_model` / `_on_thumbnail_loaded` / `on_media_loaded`
  / `on_reconcile_done` / `_render_list` / `_reset_list_for_render`
  / `_maybe_load_all_thumbs` / `_maybe_load_thumb_for_row`
  / `_make_thumb_click_handler` / `_row_widget_size_hint`
  / `_build_row_widget` / `_build_thumb_cell` / `_build_meta_cells`
  / `_build_action_buttons_in_row` / `_apply_row_hover_style`
  / `on_download_progress`
- 持有的状态:`_rows` / `_last_keys` / `_thumb_labels` / `_status_labels`
  / `_thumb_cache` / `_vm`(全部由 facade `__init__` 单点赋值)
- 持有的 widget:`list` / `lbl_status` / `btn_prune`


MRO:`class MediaManagerWidget(FilterMixin, RenderMixin, ActionsMixin,
QWidget)` —— `QWidget` 在末尾。本 mixin 是 plain class,**不写
`__init__`**:实例字段全由 facade 单点赋值(沿用
`viewmodels/_base.py:94-103` 单点 init 惯例 —— 类体写 mutable default
会被多实例共享,plan-batch9-η 为此红过 3 个 test)。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QMenu,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MediaType, MessageDTO
from tgmonitor.ui.icon import tinted_action_icon, tinted_action_pixmap
from tgmonitor.ui.thumbnail import cache_key_for, render_pixmap
from tgmonitor.ui.widgets._mm_base import (
    _MEDIA_ICONS,
    _MEDIA_TYPE_ICON_NAMES,
    _STATUS_TEXT,
    LIGHTBOX_PREVIEWABLE_TYPES,
    _channel_name,
    _format_size,
    _RowKey,
)

if TYPE_CHECKING:
    _Base = QWidget
else:
    _Base = object

log = logging.getLogger(__name__)


class _ProgressThumb(QLabel):
    """带方形环绕下载进度的缩略图 label —— 2026-11-23 v2.0.9。

    进度沿 40×40 缩略图的**矩形外框**绘制(从顶边中点顺时针绕四边),不再用
    内切圆弧 —— 圆弧四角留空,视觉上不像“围绕缩略图”;方形边框贴住四边。
    底轨道整圈常驻(颜色足够深,浅蓝选中行上也看得见四边),蓝色进度按百分比
    覆盖;下载期间状态列保持原状态显示。未设进度(`progress=None`)时完全不画,
    与旧 QLabel 无差异。

    内容必须比边框小一圈才能被“包住”:`setPixmap` 把图缩到 32×32(框内缘
    约 36px)。2026-11-24 起未下载的占位也走 32×32 类型图标,不再用会
    溢出边框的中文文字(那正是当初「框没包住缩略图」的成因之一)。

    `set_progress()` 只存值 + `update()`,真正的绘制在 `paintEvent` 走 QPainter,
    保持 UI 渲染在主线程(进度事件本身已由 VM signal 切回主线程)。
    """

    _RING_COLOR = QColor("#5b9cf5")
    _TRACK_COLOR = QColor("#9fb0c6")
    _RING_WIDTH = 3
    _CONTENT_SIZE = 32

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self._progress: int | None = None

    def setPixmap(self, pixmap: QPixmap | QImage) -> None:  # noqa: N802 — Qt 命名
        """缩略图缩到 32×32 再显示,给 36px 进度框留出一圈可见边距。"""
        source = QPixmap.fromImage(pixmap) if isinstance(pixmap, QImage) else pixmap
        if source.width() == self._CONTENT_SIZE and source.height() == self._CONTENT_SIZE:
            # 已经是目标尺寸就别过 scaled() —— 即使同尺寸重采样也会让线条发虚。
            super().setPixmap(source)
            return
        scaled = source.scaled(
            self._CONTENT_SIZE,
            self._CONTENT_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        super().setPixmap(scaled)

    def set_progress(self, percent: int | None) -> None:
        """设置 0..100 的进度;None = 清除进度描边。"""
        self._progress = percent
        self.update()

    def progress(self) -> int | None:
        return self._progress

    @staticmethod
    def _progress_segments(rect: QRectF, percent: int) -> list[tuple[QPointF, QPointF]]:
        """从顶边中点顺时针沿矩形边框走 `percent`% 周长 → 轴对齐线段列表。

        顶点序列:顶边中点 → 右上 → 右下 → 左下 → 左上 → 顶边中点;
        每段水平/垂直(方形,非圆弧),总长 = 2*(w+h)。`percent<=0` 返 []。
        """
        mid_top = QPointF(rect.center().x(), rect.top())
        corners = [
            mid_top,
            QPointF(rect.right(), rect.top()),
            QPointF(rect.right(), rect.bottom()),
            QPointF(rect.left(), rect.bottom()),
            QPointF(rect.left(), rect.top()),
            mid_top,
        ]
        perimeter = 2.0 * (rect.width() + rect.height())
        remaining = perimeter * max(0, min(100, percent)) / 100.0
        segments: list[tuple[QPointF, QPointF]] = []
        for start, end in zip(corners, corners[1:], strict=False):
            if remaining <= 0:
                break
            dx = end.x() - start.x()
            dy = end.y() - start.y()
            seg_len = abs(dx) + abs(dy)
            if seg_len <= 0:
                continue
            if remaining >= seg_len:
                segments.append((start, end))
                remaining -= seg_len
            else:
                ratio = remaining / seg_len
                segments.append((start, QPointF(start.x() + ratio * dx, start.y() + ratio * dy)))
                remaining = 0.0
        return segments

    def paintEvent(self, event: object) -> None:  # noqa: N802 — Qt 命名
        super().paintEvent(event)  # type: ignore[arg-type]
        if self._progress is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 边框中心内缩 2px,pen 宽 3 → 覆盖 0.5..3.5px,不越出 40×40 label。
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        pen = QPen(self._TRACK_COLOR)
        pen.setWidth(self._RING_WIDTH)
        painter.setPen(pen)
        painter.drawRect(rect)
        pct = max(0, min(100, self._progress))
        if pct <= 0:
            return
        pen = QPen(self._RING_COLOR)
        pen.setWidth(self._RING_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        for start, end in self._progress_segments(rect, pct):
            painter.drawLine(start, end)


# 2026-11-24:占位图标走 SVG 直渲(tinted_action_pixmap)而非 QIcon.pixmap ——
# QIcon 只注册 16/20/24 三档,取 32px 会拿到 24×24 位图再被放大,边缘发虚。
# 中性灰避免和蓝色下载动作图标混淆;真实图加载后直接 setPixmap 覆盖。
_TYPE_ICON_TINT = QColor("#7b8494")


def _media_type_icon_pixmap(media_type: MediaType) -> QPixmap:
    """按媒体类型生成原生 32×32 占位图标;未知类型退文档图标。"""
    name = _MEDIA_TYPE_ICON_NAMES.get(media_type, "media_type_document")
    size = _ProgressThumb._CONTENT_SIZE
    return tinted_action_pixmap(name, _TYPE_ICON_TINT, size)


class RenderMixin(_Base):
    """行渲染管线 — 2026-10-20 plan-batch10-δ。

    公开 API 1:1 保留原 `MediaManagerWidget` 渲染域:
    - 3 public method:`set_view_model` / `on_media_loaded` /
      `on_reconcile_done` / `on_download_progress`
    - 12 private method:列表重渲 / 缩略图 / 行构建 / meta 列 / 行内按钮

    facade **不写同名 delegate** —— test 直接调 `widget.on_media_loaded(...)`
    / `widget.on_reconcile_done(...)`,MRO 直达本 mixin 方法,调用点 0 改动。
    """

    # ---- 公共槽(MainWindow 调) ----

    def set_view_model(self, vm: object) -> None:
        """MainWindow 注入 VM — 绑 thumbnail_loaded signal + 缓存 vm 引用。

        vm 故意是 `object` 类型,widget 不引入 VM import 依赖(架构上 VM → widget
        单向)。重渲前清空旧的 thumb_labels 引用避免 stale。
        """
        self._vm = vm
        try:
            vm.thumbnail_loaded.connect(self._on_thumbnail_loaded)
        except Exception:
            log.exception("set_view_model: connect thumbnail_loaded failed")

    def _on_thumbnail_loaded(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
        data: bytes,
    ) -> None:
        """VM.thumbnail_loaded → 渲染 QPixmap + 找行 + 写 LRU + setPixmap。

        2026-10-15 plan-batch6-2:VM signal payload 改 `bytes`(Qt metatype
        `QByteArray` 跨线程安全);QPixmap 渲染下沉到 widget 端,VM 退回
        纯 EventBus+signal 适配器,符合「UI 只走 AppService facade + DTO,
        VM 不做 Qt 像素渲染」的分层约束。

        row 已不在 list(被删/重渲)→ 仍写 LRU,下次同样 key 命中。
        """
        if not data:
            return
        pix = render_pixmap(data)
        if pix is None:
            return
        key = _RowKey(channel_id, telegram_msg_id, media_idx)
        # 1) 写 LRU
        row = self._find_row_by_key(key)
        if row is not None:
            med = row[2]
            ck = cache_key_for(med)
            if ck is not None:
                self._thumb_cache.put(ck[0], ck[1], pix)
        # 2) 命中 label → setPixmap
        label = self._thumb_labels.get(key)
        if label is None:
            return
        # 清掉 emoji text,设置 pixmap;aspect-ratio 保留(已在 cache 层 scaled)
        label.setText("")
        label.setPixmap(pix)

    def on_media_loaded(self, payload: object) -> None:
        """VM.media_list_loaded 接到 → 渲染 list + 更新分页 + footer。

        2026-08-25 v1.3.0 PR #6:payload 改成 `(rows, total)` tuple —
        total 驱动 `lbl_page` / `btn_prev` / `btn_next`。
        """
        if isinstance(payload, tuple) and len(payload) == 2:
            rows, total = payload
        elif isinstance(payload, list):  # 旧版只发 list — 兜底
            rows, total = payload, len(payload)
        else:
            rows, total = [], 0
        self._rows = list(rows) if isinstance(rows, list) else []
        self._total = int(total) if isinstance(total, int) else len(self._rows)
        page_clamped = self._render_page_info()
        self._render_list(reset_scroll=page_clamped)

    def on_reconcile_done(self, evt: object) -> None:
        """VM.media_reconcile_done 接到 → 更新 footer + 显示结果。

        2026-09-03 v1.5.4 PR #P3:**修 S3 stale tooltip** — 旧版
        `backend == "s3"` 分支 setEnabled(False) + setToolTip("S3 backend:
        orphan reconcile not supported (iter_keys TODO)"),但实际
        `objectstore/s3_store.py:185` 真的有 `iter_keys`,且
        `media_service.reconcile_orphans` 走 `hasattr(self._objects, "iter_keys")`
        识别 — S3 backend 现在**真**支持 prune,与本地 FS 行为一致。
        一律 enabled + 正确 tooltip,避免误导用户「不敢点 Prune」。

        reconcile 失败仍走 `app.bus.publish(ErrorOccurred)`,UI 走既有
        toast 错误提示路径 — 本方法不感知后端可用性(那由 storage 决定)。
        """
        try:
            backend = getattr(evt, "backend", "")
            scanned = getattr(evt, "scanned", 0)
            referenced = getattr(evt, "referenced", 0)
            orphans = getattr(evt, "orphans", 0)
            deleted = getattr(evt, "deleted", 0)
            dry_run = getattr(evt, "dry_run", True)
        except Exception:
            log.exception("on_reconcile_done parse evt failed")
            return

        # 2026-09-03 v1.5.4 PR #P3:删 S3 stale 灰按钮分支 — 统一 enabled
        # + 与 __init__ 一致的 tooltip(backend 名拼进去,信息更明确)。
        self.btn_prune.setEnabled(True)
        self.btn_prune.setToolTip(self._tr_prune_scan_fmt().format(backend=backend))

        # 更新 status
        verb = "Scanned" if dry_run else "Pruned"
        self.lbl_status.setText(
            f"Reconcile ({backend}): {verb}={scanned} · referenced={referenced} · "
            f"orphans={orphans} · deleted={deleted} {'(dry run)' if dry_run else ''}"
        )

    # ---- 渲染 ----

    def _render_list(self, reset_scroll: bool | None = None) -> None:
        """清空 list → 按 rows 重建 item + 自绘 widget;更新 status label。

        2026-11-22 修:整个重建过程屏蔽 `itemSelectionChanged`。`list.clear()`
        会同步发选中变化,此时 `_rows` 已是新数据、`_last_keys` 还是旧索引,
        `_on_selection_changed → _find_row_by_key` 拿旧 key 的索引越界崩溃。
        渲染完一次性解屏蔽,由末尾的 `_on_selection_changed()` 统一刷新按钮态。

        2026-11-24 修:`list.clear()` 还会把滚动条归零 —— 下载完成触发原地
        refresh 时用户正看着的行会突然跳到列表首行。这里在重建前存下纵向/
        横向滚动值,重建后恢复;筛选/翻页由 `_mm_filter` 预先打
        `_reset_scroll_on_render=True`,这类"换页"仍从顶部看起。
        """
        v_scroll = self.list.verticalScrollBar().value()
        h_scroll = self.list.horizontalScrollBar().value()
        # reset_scroll=True 来自页码钳制:本次与随后的补拉都要回顶部。
        # stored 标记消费掉;钳制标记留给下一次补拉渲染。
        stored_reset = bool(getattr(self, "_reset_scroll_on_render", True))
        reset = stored_reset or bool(reset_scroll)
        preserve_scroll = not reset
        self._reset_scroll_on_render = bool(reset_scroll)

        self.list.blockSignals(True)
        try:
            self._reset_list_for_render()

            total_bytes = 0
            count_done = 0
            count_failed = 0

            for msg, idx, med in self._rows:
                key = _RowKey(msg.channel_id, msg.telegram_msg_id, idx)
                self._last_keys.append(key)
                item = QListWidgetItem()
                # 行高 = 自绘 widget 高度(后面 setSizeHint 调)
                item.setSizeHint(self._row_widget_size_hint())
                self.list.addItem(item)
                row_w = self._build_row_widget(msg, idx, med, key)
                self.list.setItemWidget(item, row_w)

                if med.download_status == MediaDownloadStatus.DONE:
                    count_done += 1
                    if med.file_size:
                        total_bytes += med.file_size
                elif med.download_status == MediaDownloadStatus.FAILED:
                    count_failed += 1

            # 重建完成再写回目标值(此时 scroll range 已按新 item 更新);
            # 原地 refresh 用保存值,换页/换筛选写 0 显式回到首行。
            # 行数变少时 setValue 自动钳到新最大值,不会越界。
            self.list.verticalScrollBar().setValue(v_scroll if preserve_scroll else 0)
            self.list.horizontalScrollBar().setValue(h_scroll if preserve_scroll else 0)

            # 更新 footer 状态
            total = len(self._rows)
            self.lbl_status.setText(
                self._tr_status_row().format(
                    total=total,
                    done=count_done,
                    failed=count_failed,
                    size=_format_size(total_bytes) if total_bytes else "0B",
                )
            )
        finally:
            self.list.blockSignals(False)

        # 选中变化 → enable/disable toolbar(屏蔽期间的信号在这里补一次)
        self._on_selection_changed()

        # 缩略图加载(2026-08-25 PR #1):只有 PHOTO/VIDEO + DONE 才值得加载;
        # audio/document/sticker 等保持 emoji。先查 LRU,命中直接 setPixmap,
        # miss 再触发 vm.load_thumbnail 异步。
        self._maybe_load_all_thumbs()

    def _reset_list_for_render(self) -> None:
        """重渲前清空 list + 三个 row 容器,避免旧 label 跨渲染持有引用。

        2026-11-22 v2.0.9 FR-03:`list.clear()` 只移除 item,挂载的 self-drawn
        widget 不会立即析构,可能在 viewport 残留幽灵行。对齐频道页 `_clear_rows`
        (`channel_widget.py:348-355`)先对 itemWidget 调 `deleteLater()`。
        """
        # 2026-11-22 修:先清索引再 `list.clear()` —— clear 同步触发
        # itemSelectionChanged,若信号屏蔽被别处绕过,空 _last_keys 也不会
        # 让旧 key 索引落到新 _rows 上。
        self._last_keys: list[_RowKey] = []
        self._thumb_labels.clear()  # 旧 label 全部失效
        self._status_labels.clear()  # 2026-09-01 v1.5.1 PR #B3:同上
        self._primary_buttons.clear()  # 2026-11-23:旧主按钮引用全部失效
        for i in range(self.list.count()):
            old_item = self.list.item(i)
            old_widget = self.list.itemWidget(old_item) if old_item is not None else None
            if old_widget is not None:
                old_widget.deleteLater()
        self.list.clear()

    def _maybe_load_all_thumbs(self) -> None:
        """2026-08-25 PR #1:每行触发缩略图处理(LRU 命中即时 setPixmap,miss 异步)。"""
        for row in self._rows:
            self._maybe_load_thumb_for_row(*row)

    def _maybe_load_thumb_for_row(
        self,
        msg: MessageDTO,
        idx: int,
        med: MediaDTO,
    ) -> None:
        """单行缩略图处理:cache hit 即时 setPixmap;miss 触发 VM 异步加载。"""
        # 只对 photo / video + DONE 加载;audio/document/sticker 不参与
        if med.download_status != MediaDownloadStatus.DONE:
            return
        if med.type not in (
            MediaType.PHOTO,
            MediaType.VIDEO,
            MediaType.VIDEO_NOTE,
            MediaType.ANIMATION,
            MediaType.STICKER,
        ):
            return
        ck = cache_key_for(med)
        if ck is None:
            return
        # 命中 → 即时写 label
        cached = self._thumb_cache.get(ck[0], ck[1])
        key = _RowKey(msg.channel_id, msg.telegram_msg_id, idx)
        label = self._thumb_labels.get(key)
        if cached is not None and label is not None:
            label.setText("")
            label.setPixmap(cached)
            return
        # miss → 触发 VM 加载
        if self._vm is not None:
            try:
                self._vm.load_thumbnail(
                    msg.channel_id,
                    msg.telegram_msg_id,
                    idx,
                    med,
                )
            except Exception:
                log.exception("vm.load_thumbnail dispatch failed")

    def _make_thumb_click_handler(self, key: _RowKey, thumb: QLabel) -> Callable[[Any], None]:
        """2026-08-31 v1.5.0 PR #A8:缩略图点击 → 发 preview_requested。

        用闭包绑死 `_RowKey` + 缩略图 QLabel,monkeypatch 到
        `thumb.mousePressEvent`(直接覆盖 Qt 实例方法,Qt 不推荐但 MVP
        够用;真要标准做就上自定义 QLabel 子类 `_ClickableThumb`)。
        返回的 handler 走 lambda(我们忽略 event 参数,只发信号)。
        """
        # 2026-08-31 v1.5.0 PR #A8:handler 必须返回 None(mousePressEvent 返
        # None 表示事件已接受),否则 Qt 会报「return type mismatch」。
        from PySide6.QtGui import QMouseEvent

        def _handler(event: QMouseEvent) -> None:
            if event is None:
                return
            if event.button() != Qt.LeftButton:
                return
            self.preview_requested.emit(key.channel_id, key.telegram_msg_id, key.media_idx)

        return _handler

    def _row_widget_size_hint(self) -> Any:
        """单行自绘 widget 的 size hint(高度 = 缩略图 40px + 上下 margin)。

        2026-11-22 v2.0.9:宽度 800→760 —— 行尾按钮从 5 个文字压到 4 个图标后
        不再需要那么宽,窄窗口优先保文件名列(唯一 Expanding 列)。
        """
        from PySide6.QtCore import QSize

        return QSize(760, 48)

    def _build_row_widget(
        self,
        msg: MessageDTO,
        idx: int,
        med: MediaDTO,
        key: _RowKey,
    ) -> QWidget:
        """单行自绘:缩略图 [频道] 类型·日期 文件名 大小 状态 [下载/预览][⋯]。"""
        w = QWidget()
        w.setObjectName("mediaRow")
        hbox = QHBoxLayout(w)
        hbox.setContentsMargins(8, 4, 8, 4)
        hbox.setSpacing(8)
        self._build_thumb_cell(hbox, med, key)
        self._build_meta_cells(hbox, msg, med, key)
        self._build_action_buttons_in_row(hbox, med, key)
        self._apply_row_hover_style(w)
        return w

    def _build_thumb_cell(self, hbox: QHBoxLayout, med: MediaDTO, key: _RowKey) -> None:
        """缩略图列 — 40×40,默认按类型显示图标;真实缩略图加载后 setPixmap 覆盖。

        2026-11-24 用户选型:第 2 列已有中文类型文字,缩略图不再重复显示
        「视频」/「文档」等文字占位,改用对应媒体类型的单色线条图标。
        2026-08-31 v1.5.0 PR #A8:图片类 DONE 时可点开 lightbox;未下载保持
        默认箭头 + 不响应。
        """
        thumb = _ProgressThumb()
        thumb.setFixedSize(40, 40)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setPixmap(_media_type_icon_pixmap(med.type))
        thumb.setToolTip(_MEDIA_ICONS.get(med.type, "附件"))
        self._thumb_labels[key] = thumb
        if (
            med.type in LIGHTBOX_PREVIEWABLE_TYPES
            and med.download_status == MediaDownloadStatus.DONE
        ):
            thumb.setCursor(Qt.PointingHandCursor)
            thumb.setToolTip(self._tr_click_preview())
            thumb.mousePressEvent = self._make_thumb_click_handler(  # type: ignore[method-assign]
                key, thumb
            )
        hbox.addWidget(thumb)

    def _build_meta_cells(
        self,
        hbox: QHBoxLayout,
        msg: MessageDTO,
        med: MediaDTO,
        key: _RowKey,
    ) -> None:
        """5 列元数据:channel / 类型+日期 / filename(扩展) / size / status。

        2026-09-01 v1.5.1 PR #B3:status 列 label 存到 self._status_labels,
        on_download_progress 事件按 _RowKey 定位行 + setText。
        2026-11-23:第 2 列从「#msg id + 英文类型」改为「中文类型 · MM-DD」,
        完整消息号 + 时间放 tooltip;文件名缺失时留空(不再写 "(no name)")。
        """
        # 频道名优先(2026-11-22 修);完整 @username / #id 信息放 tooltip,
        # 不占列表横向空间。未缓存的频道才退 #id。
        ch = self._monitored_channels.get(msg.channel_id)
        ch_label = _channel_name(ch) if ch else f"#{msg.channel_id}"
        lbl_channel = QLabel(ch_label)
        lbl_channel.setFixedWidth(140)
        lbl_channel.setToolTip(ch.display if ch else ch_label)
        hbox.addWidget(lbl_channel)

        # 类型 + 日期(2026-11-23 用户选型:中文类型 + MM-DD,消息号进 tooltip)
        type_label = _MEDIA_ICONS.get(med.type, "附件")
        date_label = msg.date.strftime("%m-%d") if msg.date else ""
        lbl_msg = QLabel(f"{type_label} · {date_label}")
        lbl_msg.setFixedWidth(90)
        lbl_msg.setStyleSheet("color: #555;")
        lbl_msg.setToolTip(
            f"#{msg.telegram_msg_id} · {type_label} · {msg.date:%Y-%m-%d %H:%M}"
            if msg.date
            else f"#{msg.telegram_msg_id} · {type_label}"
        )
        hbox.addWidget(lbl_msg)

        # filename —— 无名称留空,不写占位文案(2026-11-23 用户要求)
        name = med.file_name or ""
        lbl_name = QLabel(name)
        lbl_name.setToolTip(name)
        lbl_name.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        hbox.addWidget(lbl_name, 1)

        # size
        lbl_size = QLabel(_format_size(med.file_size))
        lbl_size.setFixedWidth(80)
        lbl_size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lbl_size.setStyleSheet("color: #777;")
        hbox.addWidget(lbl_size)

        # status(2026-09-01 v1.5.1 PR #B3:扩宽到 80px 容纳「X / Y (Z%)」进度文字)
        status_text = _STATUS_TEXT.get(med.download_status, "?")
        lbl_status = QLabel(status_text)
        lbl_status.setFixedWidth(80)
        lbl_status.setAlignment(Qt.AlignCenter)
        if med.download_status == MediaDownloadStatus.FAILED:
            lbl_status.setStyleSheet("color: #c0392b;")
            if med.download_error:
                lbl_status.setToolTip(self._tr_error_fmt().format(err=med.download_error))
        elif med.download_status == MediaDownloadStatus.DONE:
            lbl_status.setStyleSheet("color: #27ae60;")
        elif med.download_status in (
            MediaDownloadStatus.DOWNLOADING,
            None,
        ):
            # 2026-11-24:取消沙漏 + 未下载改图标 —— 两者统一显示灰色纯云形
            # (原文件只在远端、本地未落地;无向下箭头,与行尾下载动作图标区分),
            # 文字说明放 tooltip。真正在下载时进度由 on_download_progress 先清
            # 图标再写 "X / Y (Z%)";卡死残留由启动自愈复位为 None,视觉一致。
            hint = self._tr_pending_hint()
            if med.download_error:
                hint = f"{hint}（{med.download_error}）"
            lbl_status.setToolTip(hint)
            lbl_status.setText("")
            lbl_status.setPixmap(
                tinted_action_icon("media_pending", QColor("#8a8d92")).pixmap(16, 16)
            )
        self._status_labels[key] = lbl_status
        hbox.addWidget(lbl_status)

    # 行操作图标统一色调(与频道页 #channelRowAction 同色,见 channel_widget.py:383)。
    _ROW_ACTION_TINT = QColor("#5b9cf5")

    def _make_row_action_button(
        self,
        object_name: str,
        icon_name: str,
        tooltip: str,
        enabled: bool,
        on_clicked: Callable[[bool], None] | None = None,
    ) -> QToolButton:
        """常驻行操作图标按钮(QToolButton + tinted 图标 + objectName + tooltip)。

        2026-11-22 v2.0.9 FR-03:替换原文字 QPushButton,省横向空间。图标走
        `tinted_action_icon()` 注入固定色(QSS 无法直接给 QToolButton 图标上色)。
        """
        btn = QToolButton()
        btn.setObjectName(object_name)
        btn.setIcon(tinted_action_icon(icon_name, self._ROW_ACTION_TINT))
        btn.setToolTip(tooltip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setEnabled(enabled)
        if on_clicked is not None:
            btn.clicked.connect(on_clicked)
        return btn

    def _build_action_buttons_in_row(
        self,
        hbox: QHBoxLayout,
        med: MediaDTO,
        key: _RowKey,
    ) -> None:
        """行尾操作:1 个「下载 / 预览」主图标 + ⋯ 菜单(Reveal/Copy/Delete)。

        2026-11-23 v2.0.9 二轮:用户裁决「下载与打开互斥,合并为一个」——
        下载与打开不再同时出现。主按钮按当前行实时状态分派三件事:
        - DONE       → preview_requested(lightbox,和点缩略图同路径;避开 open_url
                       依赖本地落盘文件/系统关联而「无法打开」的老问题)
        - FAILED     → retry_requested(force 重下)
        - None/DOWNLOADING → download_requested(首下)

        图标 / tooltip 在渲染时按 med 状态设定(DONE=眼睛图标「预览」,其余=下载
        图标)。点击分派查当前行实时状态,不跨行缓存。菜单项在弹出前按当前行重算。

        2026-11-23 防重复:点下载/重下后立刻禁用本按钮(不等进度事件),下载中
        不接受二次点击。完成/失败靠 `MediaDownloaded` 触发的列表重渲自动恢复
        (DONE→预览,FAILED→下载);core 层 `_guard_inflight` 再挡并发入口。
        """
        status = med.download_status
        is_done = status == MediaDownloadStatus.DONE
        # 闭包持有主按钮引用(按钮在下方创建后 append),供点击时即时禁用。
        primary_btn: list[QToolButton] = []

        def _disable_for_download() -> None:
            """发起下载后即时禁用本行按钮,tooltip 提示进行中。"""
            if not primary_btn:
                return
            btn = primary_btn[0]
            btn.setEnabled(False)
            btn.setToolTip("下载中…（完成后自动恢复）")

        def emit_primary(_checked: bool = False, k: _RowKey = key) -> None:
            """按当前行实时状态分派 预览 / 重下 / 首下(防跨行缓存)。"""
            row = self._find_row_by_key(k)
            cur = row[2].download_status if row is not None else status
            if cur == MediaDownloadStatus.DONE:
                self.preview_requested.emit(k.channel_id, k.telegram_msg_id, k.media_idx)
            elif cur == MediaDownloadStatus.FAILED:
                self.retry_requested.emit(k.channel_id, k.telegram_msg_id, k.media_idx)
                _disable_for_download()
            else:  # None / DOWNLOADING
                self.download_requested.emit(k.channel_id, k.telegram_msg_id, k.media_idx)
                _disable_for_download()

        def emit_reveal(_checked: bool = False, k: _RowKey = key) -> None:
            self.reveal_requested.emit(k.channel_id, k.telegram_msg_id, k.media_idx)

        def emit_copy(_checked: bool = False, k: _RowKey = key) -> None:
            self.copy_requested.emit(k.channel_id, k.telegram_msg_id, k.media_idx)

        def emit_delete(_checked: bool = False, k: _RowKey = key) -> None:
            self.delete_requested.emit(k.channel_id, k.telegram_msg_id, k.media_idx)

        # 单一主图标:下载 / 预览互斥,按状态切图标 + tooltip,点击按实时状态分派
        primary_icon = "media_preview" if is_done else "media_download"
        if is_done:
            primary_tip = "预览文件"
        elif status == MediaDownloadStatus.DOWNLOADING:
            # 2026-11-23:残留/中断的 DOWNLOADING(进程崩溃后自愈会复位,
            # 自愈前点这里可续传)— 明确告知用户中断后可手动重下。
            primary_tip = "继续下载（中断后可手动重下）"
        else:
            primary_tip = "下载原文件（失败项将重新下载）"
        primary = self._make_row_action_button(
            "mediaActionDownload", primary_icon, primary_tip, True, emit_primary
        )
        primary_btn.append(primary)
        self._primary_buttons[key] = primary
        hbox.addWidget(primary)

        # ⋯ 菜单:低频操作 Reveal / Copy / Delete。菜单项 emit 与常驻图标同一组 Signal。
        # 2026-11-22 修:不再 setMenu —— QToolButton 一旦挂 menu,无论 QSS 怎么
        # 压 ::menu-button,原生样式仍会在 ⋯ 右侧再画一个下箭头。改成 clicked
        # 时手动 popup,既只有一个图标,又保留同一份 QMenu(作为 btn 子对象)。
        btn_more = self._make_row_action_button("mediaRowMore", "media_more", "更多操作", True)
        menu = QMenu(btn_more)
        menu.setObjectName("mediaRowMenu")
        act_reveal = menu.addAction(
            tinted_action_icon("media_reveal", self._ROW_ACTION_TINT),
            "在文件管理器中显示",
        )
        act_reveal.triggered.connect(emit_reveal)
        act_copy = menu.addAction(
            tinted_action_icon("media_copy", self._ROW_ACTION_TINT),
            "复制文件路径",
        )
        act_copy.triggered.connect(emit_copy)
        # 2026-11-23:删除是破坏性操作 — 分隔线单独成组 + 红色图标标识。
        menu.addSeparator()
        act_delete = menu.addAction(tinted_action_icon("media_delete", QColor("#c0392b")), "删除")
        act_delete.triggered.connect(emit_delete)

        def _refresh_menu_enabled(k: _RowKey = key) -> None:
            """弹出前按当前行状态重算 enabled —— 防跨行缓存(RK-06)。"""
            row = self._find_row_by_key(k)
            cur_status = row[2].download_status if row is not None else None
            cur_done = cur_status == MediaDownloadStatus.DONE
            act_reveal.setEnabled(cur_done)
            act_copy.setEnabled(cur_done)
            # Delete 恒可用;下载/预览(合并主按钮)在常驻图标上,不进菜单。
            act_delete.setEnabled(True)

        menu.aboutToShow.connect(_refresh_menu_enabled)

        def _show_menu(_checked: bool = False) -> None:
            """点 ⋯ 直接在按钮下方弹菜单 —— 不挂 setMenu,故无原生下箭头。"""
            menu.popup(btn_more.mapToGlobal(btn_more.rect().bottomLeft()))

        btn_more.clicked.connect(_show_menu)
        hbox.addWidget(btn_more)

    def _apply_row_hover_style(self, w: QWidget) -> None:
        """行 hover 高亮 — QSS 透明底 + hover 浅蓝。"""
        w.setStyleSheet(
            "QWidget#mediaRow { background: transparent; }"
            "QWidget#mediaRow:hover { background: #f5f7fb; }"
        )

    # ---- 内部:进度 ----

    def on_download_progress(self, event) -> None:
        """2026-11-23 v2.0.9:`MediaDownloadProgress` → 缩略图方形环绕进度。

        进度条沿缩略图矩形四边绘制(`_ProgressThumb.set_progress`),不再写状态列 ——
        状态列保持原状态显示(未下载云图标 / DONE ✓ / FAILED ×),避免下载期间
        状态列文字忽长忽短挤占布局。

        - 走 `_thumb_labels.get(key)`(行已渲染)→ set_progress(0..100)
        - `total=None`(未知大小)→ 0%,底轨道仍可见表示在进行中
        - 行被 filter 掉 / reload 中 → 静默 skip(`get` 返 None)
        - 节流:Telegram 客户端 0.5s/次,UI 端不再节流
        """
        key = _RowKey(
            channel_id=event.channel_id,
            telegram_msg_id=event.telegram_msg_id,
            media_idx=event.media_idx,
        )
        thumb = self._thumb_labels.get(key)
        if thumb is None:
            return
        d = event.downloaded
        t = event.total
        # 未知大小 → 0%(只画底轨道,表示在进行中)
        pct = int(d * 100 // t) if t and t > 0 else 0
        thumb.set_progress(pct)
        # 批量下载不走行内点击,这里兜底禁用本行按钮(完成/失败重渲自动恢复)。
        btn = self._primary_buttons.get(key)
        if btn is not None and btn.isEnabled():
            btn.setEnabled(False)
            btn.setToolTip("下载中…（完成后自动恢复）")
