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

## i18n:4 处 tr 字面量为什么不跟过来

同 `_mm_actions.py`:本 mixin 原本含 4 处 `tr()` 调用(prune tooltip /
status footer / 缩略图 tooltip / 错误 tooltip)。它们**不能**搬进本文件 ——
lupdate 静态扫描按 **enclosing class name** 定 context,写在这里会变成
`RenderMixin`,下次跑 `lupdate -extensions py` 时 `.ts` 的 38 条
`MediaManagerWidget` 翻译会被标 `type="vanished"` → `lrelease` 产出空
`.qm` → i18n 全炸(CHANGELOG:1474 事故)。

字面量留在 facade 的 `_tr_prune_scan_fmt` / `_tr_status_row` /
`_tr_click_preview` / `_tr_error_fmt` 四个 accessor,mixin 侧只调
accessor 取**已翻译**字串。

注意 `扫描 ObjectStore ({backend}) vs storage ...`(带 `{backend}` 占位)
与 toolbar 的 `扫描 ObjectStore vs storage ...`(不带占位)是**两条不同
的翻译** —— 前者是本 mixin 的 `_tr_prune_scan_fmt`,后者留在 facade 的
`_build_action_buttons` 里不动,别合并。

MRO:`class MediaManagerWidget(FilterMixin, RenderMixin, ActionsMixin,
QWidget)` —— `QWidget` 在末尾。本 mixin 是 plain class,**不写
`__init__`**:实例字段全由 facade 单点赋值(沿用
`viewmodels/_base.py:94-103` 单点 init 惯例 —— 类体写 mutable default
会被多实例共享,plan-batch9-η 为此红过 3 个 test)。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MediaType, MessageDTO
from tgmonitor.ui.thumbnail import cache_key_for, render_pixmap
from tgmonitor.ui.widgets._mm_base import (
    _MEDIA_ICONS,
    _STATUS_TEXT,
    LIGHTBOX_PREVIEWABLE_TYPES,
    _format_size,
    _RowKey,
)

if TYPE_CHECKING:
    _Base = QWidget
else:
    _Base = object

log = logging.getLogger(__name__)


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
        except Exception:  # noqa: BLE001
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
        self._render_page_info()
        self._render_list()

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

    def _render_list(self) -> None:
        """清空 list → 按 rows 重建 item + 自绘 widget;更新 status label。"""
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
        # 选中变化 → enable/disable toolbar
        self._on_selection_changed()

        # 缩略图加载(2026-08-25 PR #1):只有 PHOTO/VIDEO + DONE 才值得加载;
        # audio/document/sticker 等保持 emoji。先查 LRU,命中直接 setPixmap,
        # miss 再触发 vm.load_thumbnail 异步。
        self._maybe_load_all_thumbs()

    def _reset_list_for_render(self) -> None:
        """重渲前清空 list + 三个 row 容器,避免旧 label 跨渲染持有引用。"""
        self.list.clear()
        # 局部标注:mypy 在 mixin 里看不到 facade `__init__` 的字段声明,
        # 空 list 赋值必须自带类型(2026-10-20 plan-batch10-δ)
        self._last_keys: list[_RowKey] = []
        self._thumb_labels.clear()  # 旧 label 全部失效
        self._status_labels.clear()  # 2026-09-01 v1.5.1 PR #B3:同上

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
            except Exception:  # noqa: BLE001
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
        """单行自绘 widget 的 size hint(高度 = 缩略图 40px + 上下 margin)。"""
        from PySide6.QtCore import QSize

        return QSize(800, 48)

    def _build_row_widget(
        self,
        msg: MessageDTO,
        idx: int,
        med: MediaDTO,
        key: _RowKey,
    ) -> QWidget:
        """单行自绘:🖼 [Channel] #msg42 type filename size status [Open][Retry][Delete]。"""
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
        """缩略图列(2026-08-25 PR #1)— 40×40,初始显 emoji,VM 异步加载完后 setPixmap 替换。

        2026-08-31 v1.5.0 PR #A8:可点击 — 图片类(photo/animation/sticker) DONE 时
        设 PointingHandCursor + 接 mousePressEvent 发 preview_requested;非图 / 未下
        载保持默认箭头 + 不响应。
        """
        thumb = QLabel(_MEDIA_ICONS.get(med.type, "📎"))
        thumb.setFixedSize(40, 40)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setStyleSheet("font-size: 22px;")
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
        """5 列元数据:channel / msg id+type / filename(扩展) / size / status。

        2026-09-01 v1.5.1 PR #B3:status 列 label 存到 self._status_labels,
        on_download_progress 事件按 _RowKey 定位行 + setText。
        """
        # channel name(优先 known_channels,否则 #channel_id)
        ch = self._known_channels.get(msg.channel_id)
        ch_label = ch.display if ch else f"#{msg.channel_id}"
        lbl_channel = QLabel(ch_label)
        lbl_channel.setFixedWidth(140)
        lbl_channel.setToolTip(ch_label)
        hbox.addWidget(lbl_channel)

        # msg id + type
        lbl_msg = QLabel(f"#{msg.telegram_msg_id} {med.type.value}")
        lbl_msg.setFixedWidth(160)
        lbl_msg.setStyleSheet("color: #555;")
        hbox.addWidget(lbl_msg)

        # filename
        name = med.file_name or "(no name)"
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
        elif med.download_status == MediaDownloadStatus.DOWNLOADING:
            # 2026-09-01 v1.5.1 PR #B3:下载中状态颜色,跟 PENDING 区分
            lbl_status.setStyleSheet("color: #2980b9;")
        self._status_labels[key] = lbl_status
        hbox.addWidget(lbl_status)

    def _build_action_buttons_in_row(
        self,
        hbox: QHBoxLayout,
        med: MediaDTO,
        key: _RowKey,
    ) -> None:
        """行内 5 个 action 按钮:Open / Reveal / Copy / Retry / Delete。

        - Open / Reveal / Copy:DONE 才可点
          - 2026-08-27 v1.4.0 PR #16:S3 后端下 Reveal 自动 disable(S3 无本地路径)
        - Retry:FAILED 才可点
        - Delete:总是可点
        """
        is_done = med.download_status == MediaDownloadStatus.DONE

        # Open
        btn_open = QPushButton("Open")
        btn_open.setFixedHeight(26)
        btn_open.setCursor(Qt.PointingHandCursor)
        btn_open.setEnabled(is_done)
        btn_open.clicked.connect(
            lambda _checked=False, k=key: self.open_requested.emit(
                k.channel_id,
                k.telegram_msg_id,
                k.media_idx,
            )
        )
        hbox.addWidget(btn_open)

        # Reveal
        btn_reveal = QPushButton("Reveal")
        btn_reveal.setFixedHeight(26)
        btn_reveal.setCursor(Qt.PointingHandCursor)
        btn_reveal.setEnabled(is_done)
        btn_reveal.clicked.connect(
            lambda _checked=False, k=key: self.reveal_requested.emit(
                k.channel_id,
                k.telegram_msg_id,
                k.media_idx,
            )
        )
        hbox.addWidget(btn_reveal)

        # Copy
        btn_copy = QPushButton("Copy")
        btn_copy.setFixedHeight(26)
        btn_copy.setCursor(Qt.PointingHandCursor)
        btn_copy.setEnabled(is_done)
        btn_copy.clicked.connect(
            lambda _checked=False, k=key: self.copy_requested.emit(
                k.channel_id,
                k.telegram_msg_id,
                k.media_idx,
            )
        )
        hbox.addWidget(btn_copy)

        # Retry
        btn_retry = QPushButton("Retry")
        btn_retry.setFixedHeight(26)
        btn_retry.setCursor(Qt.PointingHandCursor)
        btn_retry.setEnabled(med.download_status == MediaDownloadStatus.FAILED)
        btn_retry.clicked.connect(
            lambda _checked=False, k=key: self.retry_requested.emit(
                k.channel_id,
                k.telegram_msg_id,
                k.media_idx,
            )
        )
        hbox.addWidget(btn_retry)

        # Delete(总是可点)
        btn_delete = QPushButton("Delete")
        btn_delete.setFixedHeight(26)
        btn_delete.setCursor(Qt.PointingHandCursor)
        btn_delete.clicked.connect(
            lambda _checked=False, k=key: self.delete_requested.emit(
                k.channel_id,
                k.telegram_msg_id,
                k.media_idx,
            )
        )
        hbox.addWidget(btn_delete)

    def _apply_row_hover_style(self, w: QWidget) -> None:
        """行 hover 高亮 — QSS 透明底 + hover 浅蓝。"""
        w.setStyleSheet(
            "QWidget#mediaRow { background: transparent; }"
            "QWidget#mediaRow:hover { background: #f5f7fb; }"
        )

    # ---- 内部:进度 ----

    def on_download_progress(self, event) -> None:
        """2026-09-01 v1.5.1 PR #B3:`MediaDownloadProgress` 事件 → 刷新对应 row
        status 列文字。

        - 走 `_status_labels.get(key)`(行 widget 已渲染)→ setText
        - 行被 filter 掉 / reload 中 → 静默 skip(`get` 返 None)
        - 节流:TDLib 客户端 0.5s/次,UI 端不再节流(Qt coalesce 自动批量)
        """
        key = _RowKey(
            channel_id=event.channel_id,
            telegram_msg_id=event.telegram_msg_id,
            media_idx=event.media_idx,
        )
        lbl = self._status_labels.get(key)
        if lbl is None:
            return
        # 格式化「已下载 X / Y (Z%)」;`total=None` → "已下载 X / ?"
        d = event.downloaded
        t = event.total
        if t and t > 0:
            pct = int(d * 100 // t) if t > 0 else 0
            text = f"{_format_size(d)}/{_format_size(t)} ({pct}%)"
        else:
            text = f"{_format_size(d)}/?"
        lbl.setText(text)