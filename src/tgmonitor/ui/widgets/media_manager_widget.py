"""MediaManagerWidget — 第 5 导航页,列出已下载 / 下载中 / 失败的媒体,支持筛选与单 / 批量操作。

布局(从 plan):
┌────────────────────────────────────────────────────┐
│  [Channel ▼] [Type ▼] [Status ▼] [Search🔍] [Refresh]
├────────────────────────────────────────────────────┤
│  🖼 [TNews] #msg42 photo pic.jpg 1.2MB ✓ [Open][Reveal][Copy][Retry][Delete]
│  🎬 [Dev]  #msg17 video clip.mp4 5.4MB ❌ [Open][Reveal][Copy][Retry][Delete]
│  🎵 [Music] #msg99 audio song.mp3 800KB ⏳ [Open][Reveal][Copy][Retry][Delete]
├────────────────────────────────────────────────────┤
│  [Select All] [Retry Selected] [Delete Selected] [Clear Channel] [Prune Orphans]
│  Storage: 142 MB / 23 files · 1 failed
└────────────────────────────────────────────────────┘

实现要点:
- 每行是 QListWidgetItem + 自绘 widget(`setItemWidget`)显示 icon + 频道 + 类型 + 状态 + 文件名 + 大小 + 3 个按钮
- 顶部 3 个 QComboBox + 1 个 QLineEdit(search)+ 1 个 QPushButton(refresh)
- 底部 4 个 QPushButton + 1 个 QLabel(footer 状态)
- 所有用户操作 → emit Qt Signal → MainWindow 调 VM
- 后台线程安全:VM 用 run_coro 在 qasync 主 loop 触发,UI 端只读数据
"""

# mypy: disable-error-code="attr-defined"

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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
    SortDir,
    SortKey,
)
from tgmonitor.ui.widgets.thumbnail_cache import ThumbnailCache, cache_key_for

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

# 2026-08-31 v1.5.0 PR #A8:Lightbox 预览白名单 — 仅图片类(DONE 状态下)
# 可点缩略图弹大图;视频/音频/文档走 Open/Reveal,不动 lightbox。
# STICKER 算图片(WebP),ANIMATION 是 GIF。
_LIGHTBOX_PREVIEWABLE_TYPES: frozenset[MediaType] = frozenset(
    {MediaType.PHOTO, MediaType.STICKER, MediaType.ANIMATION}
)

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
    # 2026-08-27 v1.4.0 PR #16:Reveal / Copy 按钮 — 走相同的 (cid, mid, idx) 协议。
    reveal_requested = Signal(int, int, int)
    copy_requested = Signal(int, int, int)
    # 2026-08-31 v1.5.0 PR #A8:Lightbox 内嵌预览 — 点击缩略图触发,
    # MainWindow 接到后异步加载原图 bytes → QPixmap → 弹 LightboxDialog。
    preview_requested = Signal(int, int, int)

    # 批量操作 — payload 是 list[_RowKey]
    batch_retry_requested = Signal(list)
    batch_delete_requested = Signal(list)

    # 顶部刷新 / 底部 prune
    refresh_requested = Signal()
    prune_requested = Signal()  # MainWindow → 二次确认 → VM.reconcile_orphans(False)
    # 2026-08-25 v1.3.0 PR #7:Media Manager 当前视图一键导出 CSV —
    # MainWindow 接到 path 后构造 MediaExportRequest 调 vm.export_media_list。
    # Signal 带 str(out_path),UI 在 click handler 里 QFileDialog 选完文件后 emit。
    export_csv_requested = Signal(str)
    # 2026-09-01 v1.5.1 PR #B4:ZIP 打包导出 — 同 CSV 流程,但 out 走
    # .zip 扩展名;MainWindow 构造 ExportRequest(channel_ids=[本频道],
    # single_message_id=None, include_thumbnails 可选)→ vm.export_zip。
    export_zip_requested = Signal(str, bool)  # (out_path, include_thumbnails)

    # 2026-08-25 PR #4:按频道批量删除 — payload channel_id(MainWindow 收到后
    # 二次确认 + 调 vm.delete_by_channel)
    clear_channel_requested = Signal(int)

    # 频道列表变化(VM.refresh_joined_channels → 这里)
    set_channels_requested = Signal()  # MainWindow 接到就 vm.refresh_joined_channels

    def __init__(self, parent: QWidget | None = None) -> None:
        """建 filter bar + list + toolbar;filter 变化 → 自动 emit refresh_requested。"""
        super().__init__(parent)
        self.setObjectName("mediaManagerWidget")
        # VM 引用 — 类型 `object | None`(架构上不依赖 VM 具体类,避免循环 import)。
        self._vm: object | None = None

        # filter 状态(channel id list / 已知频道缓存 / 数据 / 选中集合)
        self._known_channels: dict[int, ChannelDTO] = {}
        self._rows: list[tuple[MessageDTO, int, MediaDTO]] = []
        self._last_keys: list[_RowKey] = []  # 与 _rows 一一对应,row → key
        # 分页(2026-08-25 v1.3.0 PR #6)— `_total` 从 VM payload 拿;
        # `_page` 0-based,`_page_size` 默认 50(单页足够大,大表分批看)。
        self._total: int = 0
        self._page: int = 0
        self._page_size: int = 50

        # 缩略图缓存(2026-08-25 PR #1)— 进程内 LRU,200 条
        self._thumb_cache = ThumbnailCache()
        # 当前行 → QLabel thumb 的映射;VM signal 进来时定位行用
        # 重渲后重建,所以不要跨渲染持有引用
        self._thumb_labels: dict[_RowKey, QLabel] = {}

        # VM 引用延迟注入:MainWindow 在 init 后调 set_view_model(vm) 把
        # thumbnail_loaded / load_thumbnail 绑进来;widget 不直接 import VM
        # 解耦。
        # (类型已在 __init__ 顶声明为 `object | None`,此处只赋值)

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
        """[Channel ▼] [Type ▼] [Status ▼] [Sort ▼] [Dir ▼] [Search🔍] [◀ Page N/M ▶] [Refresh]。"""
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

        # 2026-08-25 v1.3.0 PR #6:排序键 + 方向 + 分页 — 与 storage.list_media 的
        # sort / sort_dir / offset 透传
        self.cmb_sort = QComboBox()
        self.cmb_sort.setMinimumWidth(110)
        self.cmb_sort.setToolTip("排序键")
        for sk in SortKey:
            label = {"date": "Date", "size": "Size", "status": "Status"}[sk.value]
            self.cmb_sort.addItem(label, sk)
        hbox.addWidget(self.cmb_sort)

        self.cmb_dir = QComboBox()
        self.cmb_dir.setMinimumWidth(90)
        self.cmb_dir.setToolTip("排序方向")
        # 显式按 DESC, ASC 顺序加入,index 0 = DESC(DATE 的"最新优先"
        # 是 v1.2.0 既有行为;SortDir 枚举的 .value 字典序是 ASC 在前,但
        # UI 默认走 DESC)。
        for sd in (SortDir.DESC, SortDir.ASC):
            label = "↓ Desc" if sd == SortDir.DESC else "↑ Asc"
            self.cmb_dir.addItem(label, sd)
        hbox.addWidget(self.cmb_dir)

        self.edit_search = QLineEdit()
        self.edit_search.setPlaceholderText("Search filename…")
        self.edit_search.setClearButtonEnabled(True)
        self.edit_search.setMinimumWidth(220)
        hbox.addWidget(self.edit_search, 1)

        # 2026-08-25 v1.3.0 PR #6:分页导航 — 由 VM payload 的 total 计算 page count
        self.btn_prev = QPushButton("◀")
        self.btn_prev.setCursor(Qt.PointingHandCursor)
        self.btn_prev.setFixedWidth(32)
        self.btn_prev.setToolTip("上一页")
        self.btn_prev.clicked.connect(self._on_page_prev)
        hbox.addWidget(self.btn_prev)

        self.lbl_page = QLabel("1 / 1")
        self.lbl_page.setMinimumWidth(60)
        self.lbl_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hbox.addWidget(self.lbl_page)

        self.btn_next = QPushButton("▶")
        self.btn_next.setCursor(Qt.PointingHandCursor)
        self.btn_next.setFixedWidth(32)
        self.btn_next.setToolTip("下一页")
        self.btn_next.clicked.connect(self._on_page_next)
        hbox.addWidget(self.btn_next)

        self.btn_refresh = QPushButton("🔄 Refresh")
        self.btn_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_refresh.setToolTip("Reload media list (F5)")
        self.btn_refresh.clicked.connect(self.refresh_requested.emit)
        hbox.addWidget(self.btn_refresh)

        return hbox

    def _build_toolbar(self) -> QVBoxLayout:
        """[Select All] [Retry Selected] [Delete Selected] [Clear Channel] [Prune Orphans] + status label。"""
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

        # 2026-08-25 PR #4:按频道批量删除 — 清空 filter 选中频道的全部 message
        # (含 media + bytes);MainWindow 接到信号二次确认后再调 VM。
        self.btn_clear_channel = QPushButton("🗑 Clear Channel")
        self.btn_clear_channel.setCursor(Qt.PointingHandCursor)
        self.btn_clear_channel.setToolTip(
            "Delete ALL messages in the selected channel (irreversible)",
        )
        self.btn_clear_channel.clicked.connect(self._on_clear_channel)
        actions.addWidget(self.btn_clear_channel)

        # 2026-08-25 v1.3.0 PR #7:Media Manager 当前视图 → CSV 一键导出
        # (filter / sort / 全部页,不只是当前页)。MainWindow 接信号 → 构造
        # MediaExportRequest → 调 vm.export_media_list。
        self.btn_export_csv = QPushButton("📤 Export CSV")
        self.btn_export_csv.setCursor(Qt.PointingHandCursor)
        self.btn_export_csv.setToolTip(
            "Export current filter/sort view to CSV (all pages)",
        )
        self.btn_export_csv.clicked.connect(self._on_export_csv)
        actions.addWidget(self.btn_export_csv)

        # 2026-09-01 v1.5.1 PR #B4:ZIP 打包导出 — 镜像 CSV 流程,emit
        # `export_zip_requested(out_path, include_thumbnails)`。缩略图勾
        # 选用 widget 内的 `chk_zip_thumbs` 开关;MainWindow 接到后构造
        # `ExportRequest(format=ZIP, include_thumbnails=...)`。
        self.btn_export_zip = QPushButton("📦 Export ZIP")
        self.btn_export_zip.setCursor(Qt.PointingHandCursor)
        self.btn_export_zip.setToolTip(
            "Pack current filter view's media bytes + manifest.json into .zip",
        )
        self.btn_export_zip.clicked.connect(self._on_export_zip)
        actions.addWidget(self.btn_export_zip)
        # 缩略图打包开关 — 默认 unchecked(纯媒体包);只在 ZIP 按钮旁,
        # 不影响 CSV 流程。
        self.chk_zip_thumbs = QCheckBox("含缩略图")
        self.chk_zip_thumbs.setCursor(Qt.PointingHandCursor)
        self.chk_zip_thumbs.setToolTip(
            "When packing ZIP, also fetch each media's thumb_key and write thumb_<arcname>",
        )
        actions.addWidget(self.chk_zip_thumbs)

        actions.addStretch(1)

        self.btn_prune = QPushButton("🧹 Prune Orphans")
        self.btn_prune.setCursor(Qt.PointingHandCursor)
        self.btn_prune.setToolTip(
            "Scan ObjectStore vs storage and delete orphan bytes (irreversible)"
        )
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
        """filter 变化 → 自动 emit refresh_requested;search 加 300ms debounce。

        2026-08-25 v1.3.0 PR #6:sort / dir combo 变化 → 触发刷新;分页按钮
        不在这里接,各 click handler 单独绑。filter 变化(sort/dir/channel/type/
        status/search)重置 `_page=0`,避免「筛选翻页后切筛选仍跳老 offset」。
        """
        from PySide6.QtCore import QTimer

        def _reset_and_refresh() -> None:
            self._page = 0
            self.refresh_requested.emit()

        self.cmb_channel.currentIndexChanged.connect(lambda _: _reset_and_refresh())
        self.cmb_type.currentIndexChanged.connect(lambda _: _reset_and_refresh())
        self.cmb_status.currentIndexChanged.connect(lambda _: _reset_and_refresh())
        self.cmb_sort.currentIndexChanged.connect(lambda _: _reset_and_refresh())
        self.cmb_dir.currentIndexChanged.connect(lambda _: _reset_and_refresh())
        # search 不立刻触发(用户连续输入),用 debounce timer
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(_reset_and_refresh)
        self.edit_search.textChanged.connect(lambda _: self._search_timer.start())

    # ---- 分页(2026-08-25 v1.3.0 PR #6)----

    def _render_page_info(self) -> None:
        """更新 `lbl_page` 文字 + btn_prev/btn_next enabled 状态。"""
        total_pages = (
            max(1, (self._total + self._page_size - 1) // self._page_size) if self._total else 1
        )
        # 当前 page 不能超 total_pages(极端 case:total 变小后,_page 还在旧 max)
        if self._page >= total_pages:
            self._page = max(0, total_pages - 1)
        self.lbl_page.setText(f"{self._page + 1} / {total_pages}")
        self.btn_prev.setEnabled(self._page > 0)
        self.btn_next.setEnabled(self._page + 1 < total_pages)

    def _on_page_prev(self) -> None:
        """上一页 — 触发 refresh,VM 会带新 offset。"""
        if self._page <= 0:
            return
        self._page -= 1
        # 立刻同步 UI(乐观)— VM 拉回数据后 `on_media_loaded` 会再校准
        self._render_page_info()
        self.refresh_requested.emit()

    def _on_page_next(self) -> None:
        """下一页 — 触发 refresh,VM 会带新 offset。"""
        total_pages = (
            max(1, (self._total + self._page_size - 1) // self._page_size) if self._total else 1
        )
        if self._page + 1 >= total_pages:
            return
        self._page += 1
        # 立刻同步 UI(乐观)— VM 拉回数据后 `on_media_loaded` 会再校准
        self._render_page_info()
        self.refresh_requested.emit()

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
        pix: object,
    ) -> None:
        """VM.thumbnail_loaded → 找行 + 写 LRU + setPixmap。

        row 已不在 list(被删/重渲)→ 仍写 LRU,下次同样 key 命中。
        """
        if not isinstance(pix, QPixmap):
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
        """导出当前 filter 字典 — VM.load_media_list 用。

        2026-08-25 v1.3.0 PR #6 新增 `sort` / `sort_dir` / `offset` — 与
        `storage.list_media` 透传。
        """
        # sort / dir combo 的 currentData 可能是 None(刚构造时)→ 用 fallback
        sort = self.cmb_sort.currentData() if hasattr(self, "cmb_sort") else None
        sort_dir = self.cmb_dir.currentData() if hasattr(self, "cmb_dir") else None
        return {
            "channel_id": self.cmb_channel.currentData(),
            "media_type": self.cmb_type.currentData(),
            "status": self.cmb_status.currentData(),
            "search": self.edit_search.text().strip(),
            "sort": sort if isinstance(sort, SortKey) else SortKey.DATE,
            "sort_dir": sort_dir if isinstance(sort_dir, SortDir) else SortDir.DESC,
            "offset": self._page * self._page_size,
            "total": self._total,
        }

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
            self.btn_prune.setToolTip(
                "Scan ObjectStore vs storage and delete orphan bytes (irreversible)"
            )

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
        self._thumb_labels.clear()  # 旧 label 全部失效

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

        # 缩略图加载(2026-08-25 PR #1):只有 PHOTO/VIDEO + DONE 才值得加载;
        # audio/document/sticker 等保持 emoji。先查 LRU,命中直接 setPixmap,
        # miss 再触发 vm.load_thumbnail 异步。
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

        # 缩略图列(2026-08-25 PR #1)— 64×64,初始显 emoji,
        # VM 异步加载完后 setPixmap 替换。失败/非图保持 emoji。
        # 2026-08-31 v1.5.0 PR #A8:可点击 — 图片类(photo/animation/sticker)
        # DONE 时设 PointingHandCursor + 接 mousePressEvent 发 preview_requested;
        # 非图 / 未下载保持默认箭头 + 不响应。
        thumb = QLabel(_MEDIA_ICONS.get(med.type, "📎"))
        thumb.setFixedSize(40, 40)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setStyleSheet("font-size: 22px;")
        self._thumb_labels[key] = thumb
        if (
            med.type in _LIGHTBOX_PREVIEWABLE_TYPES
            and med.download_status == MediaDownloadStatus.DONE
        ):
            thumb.setCursor(Qt.PointingHandCursor)
            thumb.setToolTip("点击查看大图")
            thumb.mousePressEvent = self._make_thumb_click_handler(  # type: ignore[method-assign]
                key, thumb
            )
        hbox.addWidget(thumb)

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
                k.channel_id,
                k.telegram_msg_id,
                k.media_idx,
            )
        )
        hbox.addWidget(btn_open)

        # 2026-08-27 v1.4.0 PR #16:Reveal / Copy 按钮 — 仅 DONE 媒体可点。
        # S3 后端下 Reveal 自动 disable(S3 无本地路径);Copy 仍可用(URI 复制)。
        btn_reveal = QPushButton("Reveal")
        btn_reveal.setFixedHeight(26)
        btn_reveal.setCursor(Qt.PointingHandCursor)
        is_done = med.download_status == MediaDownloadStatus.DONE
        btn_reveal.setEnabled(is_done)
        btn_reveal.clicked.connect(
            lambda _checked=False, k=key: self.reveal_requested.emit(
                k.channel_id,
                k.telegram_msg_id,
                k.media_idx,
            )
        )
        hbox.addWidget(btn_reveal)

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

        # Retry 按钮 — FAILED 才可点
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

        # Delete 按钮 — 总是可点
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

    def _on_clear_channel(self) -> None:
        """2026-08-25 PR #4:取当前 filter 选中的 channel_id → emit 信号。

        MainWindow 接到信号做二次确认(QMessageBox.warning)再调
        vm.delete_by_channel。空 filter(All Channels)时直接 disable 按钮。
        """
        channel_id = self.cmb_channel.currentData()
        if channel_id is None:
            # `All Channels` / 未选 — 不允许整库清空
            return
        self.clear_channel_requested.emit(int(channel_id))

    def _on_export_csv(self) -> None:
        """2026-08-25 v1.3.0 PR #7:点 Export CSV → QFileDialog 选保存路径 →
        emit `export_csv_requested(str)`。
        """
        from datetime import datetime

        from PySide6.QtWidgets import QFileDialog

        default_name = f"media-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出 Media Manager 当前视图",
            default_name,
            "CSV files (*.csv)",
        )
        if not path:
            return
        self.export_csv_requested.emit(path)

    def _on_export_zip(self) -> None:
        """2026-09-01 v1.5.1 PR #B4:点 Export ZIP → QFileDialog 选 .zip 路径 →
        emit `export_zip_requested(path, include_thumbnails)`。

        `include_thumbnails` 来自同旁的 `chk_zip_thumbs` checkbox;默认
        不勾选(纯媒体包),勾上后打包 thumb_<arcname>。
        """
        from datetime import datetime

        from PySide6.QtWidgets import QFileDialog

        default_name = f"media-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出 Media Manager 当前视图为 ZIP",
            default_name,
            "ZIP files (*.zip)",
        )
        if not path:
            return
        self.export_zip_requested.emit(path, self.chk_zip_thumbs.isChecked())

    def on_channel_cleared(self, channel_id: int, deleted: int) -> None:
        """2026-08-25 PR #4:VM 反馈 → status bar + 自动 reload 当前 filter 列表。"""
        self.lbl_status.setText(
            f"Cleared channel #{channel_id}: {deleted} messages removed",
        )
        # 重 load 当前 filter(可能就是这个 channel)刷新 UI
        self.refresh_requested.emit()

    def _row_is_failed(self, key: _RowKey) -> bool:
        r = self._find_row_by_key(key)
        return r is not None and r[2].download_status == MediaDownloadStatus.FAILED
