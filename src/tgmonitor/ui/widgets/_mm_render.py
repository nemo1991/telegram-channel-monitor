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

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
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
from tgmonitor.ui.icon import tinted_action_icon
from tgmonitor.ui.thumbnail import cache_key_for, render_pixmap
from tgmonitor.ui.widgets._mm_base import (
    _MEDIA_ICONS,
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
        """清空 list → 按 rows 重建 item + 自绘 widget;更新 status label。

        2026-11-22 修:整个重建过程屏蔽 `itemSelectionChanged`。`list.clear()`
        会同步发选中变化,此时 `_rows` 已是新数据、`_last_keys` 还是旧索引,
        `_on_selection_changed → _find_row_by_key` 拿旧 key 的索引越界崩溃。
        渲染完一次性解屏蔽,由末尾的 `_on_selection_changed()` 统一刷新按钮态。
        """
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
        """单行自绘:[Image] [Channel] #msg42 type filename size status [Open][Retry][Delete]。"""
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
        thumb = QLabel(_MEDIA_ICONS.get(med.type, "附件"))
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
        # 频道名优先(2026-11-22 修);完整 @username / #id 信息放 tooltip,
        # 不占列表横向空间。未缓存的频道才退 #id。
        ch = self._monitored_channels.get(msg.channel_id)
        ch_label = _channel_name(ch) if ch else f"#{msg.channel_id}"
        lbl_channel = QLabel(ch_label)
        lbl_channel.setFixedWidth(140)
        lbl_channel.setToolTip(ch.display if ch else ch_label)
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
            lbl_status.setStyleSheet("color: #2980b9;")
            lbl_status.setToolTip(self._tr_downloading_hint())
        elif med.download_status == MediaDownloadStatus.PENDING:
            # 2026-11-22:灰字「未下载」+ 说明,区别于「正在下」的 ⏳。
            hint = self._tr_pending_hint()
            if med.download_error:
                hint = f"{hint}（{med.download_error}）"
            lbl_status.setStyleSheet("color: #8a8d92;")
            lbl_status.setToolTip(hint)
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
        """行尾操作:3 个高频图标常驻(下载/打开/重试)+ ⋯ 菜单(Reveal/Copy/Delete)。

        2026-11-22 v2.0.9 FR-03(D2=β):原 5 个文字按钮常驻占 ~838px 超 sizeHint,
        文件名列先被压没。收敛为 4 个 QToolButton 图标(~100px),低频操作进 ⋯ 菜单。

        状态规则(常驻图标在渲染时按 med 状态设 enabled;菜单项在弹出前按当前行
        重算,不跨行缓存):
        - 下载:PENDING 可点(FR-06;PR-2 只 emit,PR-5 接线 VM)
        - 打开 / Reveal / Copy:DONE 可点
        - 重试:FAILED 可点
        - Delete:总是可点
        """
        status = med.download_status
        is_done = status == MediaDownloadStatus.DONE

        def emit_download(_checked: bool = False, k: _RowKey = key) -> None:
            self.download_requested.emit(k.channel_id, k.telegram_msg_id, k.media_idx)

        def emit_open(_checked: bool = False, k: _RowKey = key) -> None:
            self.open_requested.emit(k.channel_id, k.telegram_msg_id, k.media_idx)

        def emit_retry(_checked: bool = False, k: _RowKey = key) -> None:
            self.retry_requested.emit(k.channel_id, k.telegram_msg_id, k.media_idx)

        def emit_reveal(_checked: bool = False, k: _RowKey = key) -> None:
            self.reveal_requested.emit(k.channel_id, k.telegram_msg_id, k.media_idx)

        def emit_copy(_checked: bool = False, k: _RowKey = key) -> None:
            self.copy_requested.emit(k.channel_id, k.telegram_msg_id, k.media_idx)

        def emit_delete(_checked: bool = False, k: _RowKey = key) -> None:
            self.delete_requested.emit(k.channel_id, k.telegram_msg_id, k.media_idx)

        # 常驻 3 图标
        hbox.addWidget(
            self._make_row_action_button(
                "mediaActionDownload",
                "media_download",
                "下载原文件",
                status == MediaDownloadStatus.PENDING,
                emit_download,
            )
        )
        hbox.addWidget(
            self._make_row_action_button(
                "mediaActionOpen", "media_open", "打开文件", is_done, emit_open
            )
        )
        hbox.addWidget(
            self._make_row_action_button(
                "mediaActionRetry",
                "media_retry",
                "重试下载",
                status == MediaDownloadStatus.FAILED,
                emit_retry,
            )
        )

        # ⋯ 菜单:低频操作 Reveal / Copy / Delete。菜单项 emit 与常驻图标同一组 Signal。
        # 2026-11-22 修:不再 setMenu —— QToolButton 一旦挂 menu,无论 QSS 怎么
        # 压 ::menu-button,原生样式仍会在 ⋯ 右侧再画一个下箭头。改成 clicked
        # 时手动 popup,既只有一个图标,又保留同一份 QMenu(作为 btn 子对象)。
        btn_more = self._make_row_action_button("mediaRowMore", "media_more", "更多操作", True)
        menu = QMenu(btn_more)
        menu.setObjectName("mediaRowMenu")
        act_reveal = menu.addAction("在文件管理器中显示")
        act_reveal.triggered.connect(emit_reveal)
        act_copy = menu.addAction("复制文件路径")
        act_copy.triggered.connect(emit_copy)
        act_delete = menu.addAction("删除")
        act_delete.triggered.connect(emit_delete)

        def _refresh_menu_enabled(k: _RowKey = key) -> None:
            """弹出前按当前行状态重算 enabled —— 防跨行缓存(RK-06)。"""
            row = self._find_row_by_key(k)
            cur_status = row[2].download_status if row is not None else None
            cur_done = cur_status == MediaDownloadStatus.DONE
            act_reveal.setEnabled(cur_done)
            act_copy.setEnabled(cur_done)
            # Delete 恒可用;下载/打开/重试在常驻图标上,不进菜单。
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
        """2026-09-01 v1.5.1 PR #B3:`MediaDownloadProgress` 事件 → 刷新对应 row
        status 列文字。

        - 走 `_status_labels.get(key)`(行 widget 已渲染)→ setText
        - 行被 filter 掉 / reload 中 → 静默 skip(`get` 返 None)
        - 节流:Telegram 客户端 0.5s/次,UI 端不再节流(Qt coalesce 自动批量)
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
