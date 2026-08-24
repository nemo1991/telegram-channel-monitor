"""MediaManagerWidget — 第 5 导航页,列出已下载 / 下载中 / 失败的媒体,支持筛选与单 / 批量操作。

布局(从 plan):
┌────────────────────────────────────────────────────┐
│  [Channel ▼] [Type ▼] [Status ▼] [Search🔍] [Refresh]
├────────────────────────────────────────────────────┤
│  🖼 [TNews] #msg42 photo pic.jpg 1.2MB ✓ [Open][Retry][Delete]
│  🎬 [Dev]  #msg17 video clip.mp4 5.4MB ❌ [Open][Retry][Delete]
│  🎵 [Music] #msg99 audio song.mp3 800KB ⏳ [Open][Retry][Delete]
├────────────────────────────────────────────────────┤
│  [Select All] [Retry Selected] [Delete Selected] [Prune Orphans]
│  Storage: 142 MB / 23 files · 1 failed
└────────────────────────────────────────────────────┘

实现要点:
- 每行是 QListWidgetItem + 自绘 widget(`setItemWidget`)显示 icon + 频道 + 类型 + 状态 + 文件名 + 大小 + 3 个按钮
- 顶部 3 个 QComboBox + 1 个 QLineEdit(search)+ 1 个 QPushButton(refresh)
- 底部 4 个 QPushButton + 1 个 QLabel(footer 状态)
- 所有用户操作 → emit Qt Signal → MainWindow 调 VM
- 后台线程安全:VM 用 run_coro 在 qasync 主 loop 触发,UI 端只读数据
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.dto import (
    ChannelDTO,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
)

log = logging.getLogger(__name__)


# ---------- 类型 / emoji 映射 ----------

_MEDIA_ICONS: dict[MediaType, str] = {
    MediaType.PHOTO: "🖼",
    MediaType.VIDEO: "🎬",
    MediaType.AUDIO: "🎵",
    MediaType.VOICE: "🎤",
    MediaType.DOCUMENT: "📄",
    MediaType.STICKER: "🌟",
    MediaType.ANIMATION: "🎞",
    MediaType.VIDEO_NOTE: "📹",
}

_STATUS_TEXT: dict[MediaDownloadStatus, str] = {
    MediaDownloadStatus.PENDING: "⏳",
    MediaDownloadStatus.DOWNLOADING: "⏳",
    MediaDownloadStatus.DONE: "✓",
    MediaDownloadStatus.FAILED: "❌",
}


@dataclass(frozen=True)
class _RowKey:
    """MediaRow 的稳定地址 — `(channel_id, telegram_msg_id, media_idx)`。"""

    channel_id: int
    telegram_msg_id: int
    media_idx: int


def _format_size(n: int | None) -> str:
    """人类可读大小(1.2MB / 800KB);None / 0 → '?'。"""
    if not n:
        return "?"
    units = ("B", "KB", "MB", "GB")
    size = float(n)
    unit_idx = 0
    while size >= 1024 and unit_idx < len(units) - 1:
        size /= 1024
        unit_idx += 1
    return f"{size:.1f}{units[unit_idx]}" if unit_idx > 0 else f"{int(size)}B"


# ---------- 主 widget ----------


class MediaManagerWidget(QWidget):
    """Media Manager 主页:筛选 + 列表 + 工具栏。"""

    # 单条操作 — payload 是 (channel_id, telegram_msg_id, media_idx)
    open_requested = Signal(int, int, int)
    retry_requested = Signal(int, int, int)
    delete_requested = Signal(int, int, int)

    # 批量操作 — payload 是 list[_RowKey]
    batch_retry_requested = Signal(list)
    batch_delete_requested = Signal(list)

    # 顶部刷新 / 底部 prune
    refresh_requested = Signal()
    prune_requested = Signal()  # MainWindow → 二次确认 → VM.reconcile_orphans(False)

    # 频道列表变化(VM.refresh_joined_channels → 这里)
    set_channels_requested = Signal()  # MainWindow 接到就 vm.refresh_joined_channels

    def __init__(self, parent: QWidget | None = None) -> None:
        """建 filter bar + list + toolbar;filter 变化 → 自动 emit refresh_requested。"""
        super().__init__(parent)
        self.setObjectName("mediaManagerWidget")

        # filter 状态(channel id list / 已知频道缓存 / 数据 / 选中集合)
        self._known_channels: dict[int, ChannelDTO] = {}
        self._rows: list[tuple[MessageDTO, int, MediaDTO]] = []
        self._last_keys: list[_RowKey] = []  # 与 _rows 一一对应,row → key

        self._build_ui()
        self._wire_filter()

    # ---- UI 构建 ----

    def _build_ui(self) -> None:
        """三层布局:filter bar / list / toolbar+status。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # 1) 顶部 filter bar
        root.addLayout(self._build_filter_bar())

        # 2) 中间 list(自绘 widget item)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setUniformItemSizes(False)  # 自绘行高可变
        self.list.setAlternatingRowColors(True)
        self.list.itemSelectionChanged.connect(self._on_selection_changed)
        root.addWidget(self.list, 1)

        # 3) 底部 toolbar + status
        root.addLayout(self._build_toolbar())

    def _build_filter_bar(self) -> QHBoxLayout:
        """[Channel ▼] [Type ▼] [Status ▼] [Search🔍] [Refresh]。"""
        hbox = QHBoxLayout()
        hbox.setSpacing(8)

        self.cmb_channel = QComboBox()
        self.cmb_channel.setMinimumWidth(160)
        self.cmb_channel.addItem("All channels", None)
        hbox.addWidget(self.cmb_channel)

        self.cmb_type = QComboBox()
        self.cmb_type.setMinimumWidth(120)
        self.cmb_type.addItem("All types", None)
        for mt in MediaType:
            self.cmb_type.addItem(mt.value, mt)
        hbox.addWidget(self.cmb_type)

        self.cmb_status = QComboBox()
        self.cmb_status.setMinimumWidth(120)
        self.cmb_status.addItem("All status", None)
        for st in MediaDownloadStatus:
            self.cmb_status.addItem(st.value, st)
        hbox.addWidget(self.cmb_status)

        self.edit_search = QLineEdit()
        self.edit_search.setPlaceholderText("Search filename…")
        self.edit_search.setClearButtonEnabled(True)
        self.edit_search.setMinimumWidth(220)
        hbox.addWidget(self.edit_search, 1)

        self.btn_refresh = QPushButton("🔄 Refresh")
        self.btn_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_refresh.setToolTip("Reload media list (F5)")
        self.btn_refresh.clicked.connect(self.refresh_requested.emit)
        hbox.addWidget(self.btn_refresh)

        return hbox

    def _build_toolbar(self) -> QVBoxLayout:
        """[Select All] [Retry Selected] [Delete Selected] [Prune Orphans] + status label。"""
        vbox = QVBoxLayout()
        vbox.setSpacing(6)

        # 操作行
        actions = QHBoxLayout()
        actions.setSpacing(8)

        self.btn_select_all = QPushButton("Select All")
        self.btn_select_all.setCursor(Qt.PointingHandCursor)
        self.btn_select_all.clicked.connect(self._on_select_all)
        actions.addWidget(self.btn_select_all)

        self.btn_retry_sel = QPushButton("Retry Selected")
        self.btn_retry_sel.setCursor(Qt.PointingHandCursor)
        self.btn_retry_sel.setEnabled(False)
        self.btn_retry_sel.clicked.connect(self._on_batch_retry)
        actions.addWidget(self.btn_retry_sel)

        self.btn_delete_sel = QPushButton("Delete Selected")
        self.btn_delete_sel.setCursor(Qt.PointingHandCursor)
        self.btn_delete_sel.setEnabled(False)
        self.btn_delete_sel.clicked.connect(self._on_batch_delete)
        actions.addWidget(self.btn_delete_sel)

        actions.addStretch(1)

        self.btn_prune = QPushButton("🧹 Prune Orphans")
        self.btn_prune.setCursor(Qt.PointingHandCursor)
        self.btn_prune.setToolTip("Scan ObjectStore vs storage and delete orphan bytes (irreversible)")
        self.btn_prune.clicked.connect(self.prune_requested.emit)
        actions.addWidget(self.btn_prune)

        vbox.addLayout(actions)

        # status 行
        self.lbl_status = QLabel("Loading…")
        self.lbl_status.setObjectName("mediaManagerStatus")
        self.lbl_status.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        vbox.addWidget(self.lbl_status)

        return vbox

    # ---- filter 联动 ----

    def _wire_filter(self) -> None:
        """filter 变化 → 自动 emit refresh_requested;search 加 300ms debounce。"""
        self.cmb_channel.currentIndexChanged.connect(lambda _: self.refresh_requested.emit())
        self.cmb_type.currentIndexChanged.connect(lambda _: self.refresh_requested.emit())
        self.cmb_status.currentIndexChanged.connect(lambda _: self.refresh_requested.emit())
        # search 不立刻触发(用户连续输入),用 debounce timer
        from PySide6.QtCore import QTimer

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self.refresh_requested.emit)
        self.edit_search.textChanged.connect(lambda _: self._search_timer.start())

    # ---- 公共槽(MainWindow 调) ----

    def set_known_channels(self, channels: list[ChannelDTO]) -> None:
        """VM.bootstrap_ui → MainWindow → 这里。

        重新填充 channel combo;保留当前选中。
        """
        cur = self.cmb_channel.currentData()
        self._known_channels = {c.id: c for c in channels}
        self.cmb_channel.blockSignals(True)
        self.cmb_channel.clear()
        self.cmb_channel.addItem("All channels", None)
        # 按 title 排序
        for c in sorted(channels, key=lambda x: (x.title or "").lower()):
            label = c.display
            self.cmb_channel.addItem(label, c.id)
        # 恢复选中
        if cur is not None:
            for i in range(self.cmb_channel.count()):
                if self.cmb_channel.itemData(i) == cur:
                    self.cmb_channel.setCurrentIndex(i)
                    break
        self.cmb_channel.blockSignals(False)

    def current_filters(self) -> dict[str, Any]:
        """导出当前 filter 字典 — VM.load_media_list 用。"""
        return {
            "channel_id": self.cmb_channel.currentData(),
            "media_type": self.cmb_type.currentData(),
            "status": self.cmb_status.currentData(),
            "search": self.edit_search.text().strip(),
        }

    def on_media_loaded(self, rows: object) -> None:
        """VM.media_list_loaded 接到 → 渲染 list + 更新 footer。"""
        # rows 是 list[tuple[MessageDTO, int, MediaDTO]];UI 层不强校验
        self._rows = list(rows) if isinstance(rows, list) else []  # type: ignore[arg-type]
        self._render_list()

    def on_reconcile_done(self, evt: object) -> None:
        """VM.media_reconcile_done 接到 → 更新 footer + 显示结果。

        只在 `evt.backend != 's3'` 时启用 prune 按钮;S3 用户看到灰按钮。
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

        # 启用 / 灰 prune 按钮
        if backend == "s3":
            self.btn_prune.setEnabled(False)
            self.btn_prune.setToolTip("S3 backend: orphan reconcile not supported (iter_keys TODO)")
        else:
            self.btn_prune.setEnabled(True)
            self.btn_prune.setToolTip("Scan ObjectStore vs storage and delete orphan bytes (irreversible)")

        # 更新 status
        verb = "Scanned" if dry_run else "Pruned"
        self.lbl_status.setText(
            f"Reconcile ({backend}): {verb}={scanned} · referenced={referenced} · "
            f"orphans={orphans} · deleted={deleted} {'(dry run)' if dry_run else ''}"
        )

    # ---- 渲染 ----

    def _render_list(self) -> None:
        """清空 list → 按 rows 重建 item + 自绘 widget;更新 status label。"""
        self.list.clear()
        self._last_keys = []

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
            f"{total} media · {count_done} done · {count_failed} failed · "
            f"{_format_size(total_bytes) if total_bytes else '0B'} total"
        )
        # 选中变化 → enable/disable toolbar
        self._on_selection_changed()

    def _row_widget_size_hint(self) -> Any:
        """单行自绘 widget 的 size hint(高度 36;宽度撑满)。"""
        from PySide6.QtCore import QSize

        return QSize(800, 40)

    def _build_row_widget(
        self, msg: MessageDTO, idx: int, med: MediaDTO, key: _RowKey,
    ) -> QWidget:
        """单行自绘:🖼 [Channel] #msg42 type filename size status [Open][Retry][Delete]。"""
        w = QWidget()
        w.setObjectName("mediaRow")
        hbox = QHBoxLayout(w)
        hbox.setContentsMargins(8, 4, 8, 4)
        hbox.setSpacing(8)

        # icon
        ico = QLabel(_MEDIA_ICONS.get(med.type, "📎"))
        ico.setFixedWidth(20)
        ico.setAlignment(Qt.AlignCenter)
        hbox.addWidget(ico)

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

        # status
        status_text = _STATUS_TEXT.get(med.download_status, "?")
        lbl_status = QLabel(status_text)
        lbl_status.setFixedWidth(24)
        lbl_status.setAlignment(Qt.AlignCenter)
        if med.download_status == MediaDownloadStatus.FAILED:
            lbl_status.setStyleSheet("color: #c0392b;")
            if med.download_error:
                lbl_status.setToolTip(f"Error: {med.download_error}")
        elif med.download_status == MediaDownloadStatus.DONE:
            lbl_status.setStyleSheet("color: #27ae60;")
        hbox.addWidget(lbl_status)

        # Open 按钮 — DONE 才可点
        btn_open = QPushButton("Open")
        btn_open.setFixedHeight(26)
        btn_open.setCursor(Qt.PointingHandCursor)
        btn_open.setEnabled(med.download_status == MediaDownloadStatus.DONE)
        btn_open.clicked.connect(
            lambda _checked=False, k=key: self.open_requested.emit(
                k.channel_id, k.telegram_msg_id, k.media_idx,
            )
        )
        hbox.addWidget(btn_open)

        # Retry 按钮 — FAILED 才可点
        btn_retry = QPushButton("Retry")
        btn_retry.setFixedHeight(26)
        btn_retry.setCursor(Qt.PointingHandCursor)
        btn_retry.setEnabled(med.download_status == MediaDownloadStatus.FAILED)
        btn_retry.clicked.connect(
            lambda _checked=False, k=key: self.retry_requested.emit(
                k.channel_id, k.telegram_msg_id, k.media_idx,
            )
        )
        hbox.addWidget(btn_retry)

        # Delete 按钮 — 总是可点
        btn_delete = QPushButton("Delete")
        btn_delete.setFixedHeight(26)
        btn_delete.setCursor(Qt.PointingHandCursor)
        btn_delete.clicked.connect(
            lambda _checked=False, k=key: self.delete_requested.emit(
                k.channel_id, k.telegram_msg_id, k.media_idx,
            )
        )
        hbox.addWidget(btn_delete)

        # hover 高亮
        w.setStyleSheet(
            "QWidget#mediaRow { background: transparent; }"
            "QWidget#mediaRow:hover { background: #f5f7fb; }"
        )
        return w

    # ---- 内部:选中 / 批量 ----

    def _selected_keys(self) -> list[_RowKey]:
        """当前选中的所有 row → key list(按 list 内顺序)。"""
        keys: list[_RowKey] = []
        for it in self.list.selectedItems():
            row = self.list.row(it)
            if 0 <= row < len(self._last_keys):
                keys.append(self._last_keys[row])
        return keys

    def _on_selection_changed(self) -> None:
        """enable / disable 批量按钮(选 0 → 全灰;有 FAILED → retry 可点)。"""
        sel = self._selected_keys()
        has_sel = bool(sel)
        has_failed = False
        for k in sel:
            row = self._find_row_by_key(k)
            if row is None:
                continue
            if row[2].download_status == MediaDownloadStatus.FAILED:
                has_failed = True
                break
        self.btn_retry_sel.setEnabled(has_sel and has_failed)
        self.btn_delete_sel.setEnabled(has_sel)

    def _find_row_by_key(self, key: _RowKey) -> tuple[MessageDTO, int, MediaDTO] | None:
        for i, k in enumerate(self._last_keys):
            if k == key:
                return self._rows[i]
        return None

    def _on_select_all(self) -> None:
        self.list.selectAll()

    def _on_batch_retry(self) -> None:
        keys = [k for k in self._selected_keys() if self._row_is_failed(k)]
        if keys:
            self.batch_retry_requested.emit(keys)

    def _on_batch_delete(self) -> None:
        keys = self._selected_keys()
        if keys:
            self.batch_delete_requested.emit(keys)

    def _row_is_failed(self, key: _RowKey) -> bool:
        r = self._find_row_by_key(key)
        return r is not None and r[2].download_status == MediaDownloadStatus.FAILED