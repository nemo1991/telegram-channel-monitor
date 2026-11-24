"""MediaManagerWidget — 第 5 导航页,列出已下载 / 下载中 / 失败的媒体,支持筛选与单 / 批量操作。

布局(从 plan):
┌────────────────────────────────────────────────────┐
│  [Channel ▼] [Type ▼] [Status ▼] [Search] [Refresh]
├────────────────────────────────────────────────────┤
│  [Image] [TNews] 图片 · 11-23 pic.jpg 1.2MB ✓ [预览][⋯]
│  [Video] [Dev]  视频 · 11-22 clip.mp4 5.4MB [失败] [下载][⋯]
│  [Audio] [Music] 音频 · 11-21 song.mp3 800KB 未下载 [下载][⋯]
├────────────────────────────────────────────────────┤
│  [Select All] [Retry Selected] [Delete Selected] [Clear Channel] [Prune Orphans]
│  Storage: 142 MB / 23 files · 1 failed
└────────────────────────────────────────────────────┘

实现要点:
- 每行是 QListWidgetItem + 自绘 widget(`setItemWidget`)显示缩略图 + 频道 + 类型·日期 + 文件名 + 大小 + 状态 + 1 个主图标(下载/预览) + ⋯ 菜单
- 顶部 3 个 QComboBox + 1 个 QLineEdit(search)+ 1 个 QPushButton(refresh)
- 底部 4 个 QPushButton + 1 个 QLabel(footer 状态)
- 所有用户操作 → emit Qt Signal → MainWindow 调 VM
- 后台线程安全:VM 用 run_coro 在 qasync 主 loop 触发,UI 端只读数据

"""

# mypy: disable-error-code="attr-defined"

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.dto import (
    UNDOWNLOADED_FILTER,
    ChannelDTO,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    SortDir,
    SortKey,
)
from tgmonitor.ui.icon import tinted_action_icon
from tgmonitor.ui.thumbnail import ThumbnailCache
from tgmonitor.ui.widgets._mm_actions import ActionsMixin

# 2026-10-20 plan-batch10-ε **反向 re-export**(plan §6 零改动不变量):
# `message_detail.py:36` 仍从**本 module** 导入 `LIGHTBOX_PREVIEWABLE_TYPES`。
# 10-δ 把 `_build_thumb_cell` 搬进 RenderMixin 后 facade 自身不再读它,F401
# 会把它当死 import 删掉 —— 删掉即 ImportError,13 个 test module 收集失败。
# `__all__` 里的显式声明就是 ruff 认的「有对外用途」凭据,别「顺手清理」。
from tgmonitor.ui.widgets._mm_base import (
    LIGHTBOX_PREVIEWABLE_TYPES,
    _RowKey,
)
from tgmonitor.ui.widgets._mm_filter import FilterMixin
from tgmonitor.ui.widgets._mm_render import RenderMixin

# 2026-10-20 plan-batch10-ε:facade 收口 —— 声明本 module 的公开面。
# `LIGHTBOX_PREVIEWABLE_TYPES` 是**反向 re-export**(见上方注释),列进来
# 既是对调用方的契约声明,也让 ruff F401 认得它有对外用途。
__all__ = ["LIGHTBOX_PREVIEWABLE_TYPES", "MediaManagerWidget"]

log = logging.getLogger(__name__)

# 翻页箭头与行尾图标同一色调(统一 action icon 观感)。
_PAGE_NAV_TINT = QColor("#5b9cf5")


# 2026-10-20 plan-batch10-α:`_RowKey` / `_format_size` / `_MEDIA_ICONS` /
# `_STATUS_TEXT` / `LIGHTBOX_PREVIEWABLE_TYPES` 已抽到 `widgets/_mm_base.py`
# —— 被 facade + 3 个 mixin 共用,留在本文件会造成 mixin → facade → mixin
# 循环 import。`_MEDIA_ICONS` / `_STATUS_TEXT` / `_format_size` 只被 mixin 用,
# facade 不再 import;`LIGHTBOX_PREVIEWABLE_TYPES` 见上方反向 re-export。

# ---------- 主 widget ----------


class MediaManagerWidget(FilterMixin, RenderMixin, ActionsMixin, QWidget):
    """Media Manager 主页:筛选 + 列表 + 工具栏。

    2026-10-20 plan-batch10 起本类是 **facade**(1149 → 523 LOC)。运行时
    行为拆到 3 个 mixin,本类只留「对外契约 + 静态装配 + 文案访问器」。
    `QWidget` 必须在 MRO 末尾(Shiboken 要求)。

    ## 分布索引(改代码前先看这里,别在 facade 里找运行逻辑)

    | 归属 | 文件 | 内容 |
    |---|---|---|
    | **facade(本类)** | `media_manager_widget.py` | 17 Signal(对外契约,`main_window.py` 按名 connect)+ `__init__` 单点 init(10 个实例字段)+ 15 个静态 chrome `_build_*`(再建 22 个 widget 引用)+ **11 个 `_tr_*()` 文案访问器** |
    | `FilterMixin` | `_mm_filter.py` | 6 method — 筛选联动(300ms debounce)/ 分页导航 / `current_filters` / `set_monitored_channels` |
    | `RenderMixin` | `_mm_render.py` | 16 method — VM 注入 / 缩略图 LRU / 列表重渲 / 行构建 / meta 列 / 行内按钮 / 下载进度 |
    | `ActionsMixin` | `_mm_actions.py` | 13 method — 选中集 / 批量重试·删除·下载 / 清频道 / CSV·ZIP 导出 |
    | 共享常量 | `_mm_base.py` | `_RowKey` / `_format_size` / `_MEDIA_ICONS` / `_STATUS_TEXT` / `LIGHTBOX_PREVIEWABLE_TYPES` |

    ## 三条硬约束

    1. **Signal 全留 facade**(偏离 plan-batch9 的「Signal 下沉 mixin」):
       17 个对 `main_window` 公开契约,留本类让 API 一眼可见。
       Mixin 侧只 `self.<signal>.emit(...)`,不定义也不改 Signal。
    2. **文案访问器留 facade 类体内**:mixin 侧一律调 `self._tr_xxx()`
       取字串,保持 facade 作为对外契约的唯一入口。
    3. **单点 init**:10 个实例字段全在 `__init__` 赋值,mixin **不写**
       `__init__`、类体**禁止** mutable default(`= {}` / `= []` 会跨实例共享,
       plan-batch9-η 为此红过 3 个 test)。
    """

    # 单条操作 — payload 是 (channel_id, telegram_msg_id, media_idx)
    # 2026-11-19 hotfix:channel_id 必须走 qlonglong(64 位)。`Signal(int)` 是
    # C++ 32 位 int,真实 Telegram chat_id 会让 shiboken OverflowError 污染
    # slot 的 error indicator,首个 Qt 调用(如 render_pixmap 的 QPixmap())
    # 随即 SystemError;详见 ChannelListCard.item_double_clicked 的注释。
    # 2026-11-22 v2.0.9 FR-06:下载未下载媒体原文件,同签名;PR-5 接线到 VM。
    download_requested = Signal("qlonglong", int, int)  # type: ignore[arg-type]
    open_requested = Signal("qlonglong", int, int)  # type: ignore[arg-type]
    retry_requested = Signal("qlonglong", int, int)  # type: ignore[arg-type]
    delete_requested = Signal("qlonglong", int, int)  # type: ignore[arg-type]
    # 2026-08-27 v1.4.0 PR #16:Reveal / Copy 按钮 — 走相同的 (cid, mid, idx) 协议。
    reveal_requested = Signal("qlonglong", int, int)  # type: ignore[arg-type]
    copy_requested = Signal("qlonglong", int, int)  # type: ignore[arg-type]
    # 2026-08-31 v1.5.0 PR #A8:Lightbox 内嵌预览 — 点击缩略图触发,
    # MainWindow 接到后异步加载原图 bytes → QPixmap → 弹 LightboxDialog。
    preview_requested = Signal("qlonglong", int, int)  # type: ignore[arg-type]

    # 批量操作 — payload 是 list[_RowKey]
    batch_retry_requested = Signal(list)
    batch_delete_requested = Signal(list)
    # 2026-11-22 用户选型:批量下载未下载(所选 / 当前筛选全部)
    batch_download_requested = Signal(list)
    download_all_pending_requested = Signal()

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
    clear_channel_requested = Signal("qlonglong")  # type: ignore[arg-type]

    # 频道列表变化(VM.refresh_joined_channels → 这里)
    set_channels_requested = Signal()  # MainWindow 接到就 vm.refresh_joined_channels

    def __init__(self, parent: QWidget | None = None) -> None:
        """建 filter bar + list + toolbar;filter 变化 → 自动 emit refresh_requested。"""
        super().__init__(parent)
        self.setObjectName("mediaManagerWidget")
        # VM 引用 — 类型 `object | None`(架构上不依赖 VM 具体类,避免循环 import)。
        self._vm: object | None = None

        # filter 状态(channel id list / 已知频道缓存 / 数据 / 选中集合)
        self._monitored_channels: dict[int, ChannelDTO] = {}
        self._rows: list[tuple[MessageDTO, int, MediaDTO]] = []
        self._last_keys: list[_RowKey] = []  # 与 _rows 一一对应,row → key
        # 分页(2026-08-25 v1.3.0 PR #6)— `_total` 从 VM payload 拿;
        # `_page` 0-based,`_page_size` 默认 50(单页足够大,大表分批看)。
        self._total: int = 0
        self._page: int = 0
        self._page_size: int = 50
        # 2026-11-24:下一次重渲是否从顶部开始。首次渲染 / 筛选 / 翻页为 True;
        # 下载完成等原地 refresh 为 False(保留当前滚动位置,别跳回首行)。
        self._reset_scroll_on_render: bool = True

        # 缩略图缓存(2026-08-25 PR #1)— 进程内 LRU,200 条
        self._thumb_cache = ThumbnailCache()
        # 当前行 → QLabel thumb 的映射;VM signal 进来时定位行用
        # 重渲后重建,所以不要跨渲染持有引用
        self._thumb_labels: dict[_RowKey, QLabel] = {}
        # 2026-09-01 v1.5.1 PR #B3:同模式 status 列 label — 进度事件
        # 进来时按 `(channel_id, telegram_msg_id, media_idx)` 定位 row
        # 然后 setText 刷新「已下载 X / Y (Z%)」文字。重渲时清空重建。
        self._status_labels: dict[_RowKey, QLabel] = {}
        # 2026-11-23:行尾主按钮(下载/预览)映射 — 进度事件进来时禁用本行按钮,
        # 覆盖批量下载(不走行内点击,没有即时禁用的机会)。重渲时清空重建。
        self._primary_buttons: dict[_RowKey, QToolButton] = {}

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
        # v2.0.9:objectName 让 QSS 单独覆盖通用 item 的上下 padding ——
        # 自绘行 widget 高 48px 已含 4px 上下留白,通用 item 再加 4px padding
        # + 1px border-bottom 会把 widget 底部裁掉 ~9px,方形进度框底边整条
        # 落在裁剪区外(用户报「框只包了一半」)。见 style.qss 的 mediaList 规则。
        self.list.setObjectName("mediaList")
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setUniformItemSizes(False)  # 自绘行高可变
        self.list.setAlternatingRowColors(True)
        self.list.itemSelectionChanged.connect(self._on_selection_changed)
        root.addWidget(self.list, 1)

        # 3) 底部 toolbar + status
        root.addLayout(self._build_toolbar())

    def _build_filter_bar(self) -> QHBoxLayout:
        """[Channel ▼] [Type ▼] [Status ▼] [Sort ▼] [Dir ▼] [Search搜索] [‹ Page N/M ›] [Refresh]。"""
        hbox = QHBoxLayout()
        hbox.setSpacing(8)
        hbox.addWidget(self._build_channel_filter())
        hbox.addWidget(self._build_type_filter())
        hbox.addWidget(self._build_status_filter())
        # 2026-08-25 v1.3.0 PR #6:排序键 + 方向 + 分页 — 与 storage.list_media 的
        # sort / sort_dir / offset 透传
        hbox.addWidget(self._build_sort_filter())
        hbox.addWidget(self._build_dir_filter())
        hbox.addWidget(self._build_search_box(), 1)
        # 2026-08-25 v1.3.0 PR #6:分页导航 — 由 VM payload 的 total 计算 page count
        hbox.addWidget(self._build_prev_button())
        hbox.addWidget(self._build_page_label())
        hbox.addWidget(self._build_next_button())
        # 刷新按钮(文案走 tr() 包装)
        hbox.addWidget(self._build_refresh_button())
        return hbox

    def _build_channel_filter(self) -> QComboBox:
        """频道下拉 — index 0 = None 占位(「全部频道」)。"""
        cmb = QComboBox()
        cmb.setMinimumWidth(160)
        cmb.addItem(self._tr_all_channels(), None)
        self.cmb_channel = cmb
        return cmb

    def _build_type_filter(self) -> QComboBox:
        """顶部 Type ▼ — index 0 = None 占位,后续枚举所有 MediaType。"""
        cmb = QComboBox()
        cmb.setMinimumWidth(120)
        cmb.addItem(self.tr("全部类型"), None)
        for mt in MediaType:
            cmb.addItem(mt.value, mt)
        self.cmb_type = cmb
        return cmb

    def _build_status_filter(self) -> QComboBox:
        """顶部 Status ▼ — index 0 = None 占位,「未下载」+ 三个已启动下载态。"""
        cmb = QComboBox()
        cmb.setMinimumWidth(120)
        cmb.addItem(self.tr("全部状态"), None)
        # 2026-11-22:枚举删 PENDING 后,「未下载」档用哨兵 data 表达,
        # controller 翻成 undownloaded=True 筛 download_status is None。
        cmb.addItem(self.tr("未下载"), UNDOWNLOADED_FILTER)
        for st in MediaDownloadStatus:
            cmb.addItem(st.value, st)
        self.cmb_status = cmb
        return cmb

    def _build_sort_filter(self) -> QComboBox:
        """2026-08-25 v1.3.0 PR #6:排序键 — index 0..N 一一对应 SortKey 枚举顺序。"""
        cmb = QComboBox()
        cmb.setMinimumWidth(110)
        cmb.setToolTip(self.tr("排序键"))
        for sk in SortKey:
            label = {
                "date": self.tr("日期"),
                "size": self.tr("大小"),
                "status": self.tr("状态"),
            }[sk.value]
            cmb.addItem(label, sk)
        self.cmb_sort = cmb
        return cmb

    def _build_dir_filter(self) -> QComboBox:
        """排序方向 — 显式按 DESC, ASC 顺序(index 0 = DESC 是 v1.2.0 既有行为)。

        SortDir 枚举的 .value 字典序是 ASC 在前,但 UI 默认走 DESC。
        """
        cmb = QComboBox()
        cmb.setMinimumWidth(90)
        cmb.setToolTip(self.tr("排序方向"))
        for sd in (SortDir.DESC, SortDir.ASC):
            label = self.tr("↓ 降序") if sd == SortDir.DESC else self.tr("↑ 升序")
            cmb.addItem(label, sd)
        self.cmb_dir = cmb
        return cmb

    def _build_search_box(self) -> QLineEdit:
        """文件名搜索框 — 300ms debounce 由 _wire_filter 串接。"""
        edit = QLineEdit()
        edit.setPlaceholderText(self.tr("搜索文件名…"))
        edit.setClearButtonEnabled(True)
        edit.setMinimumWidth(220)
        self.edit_search = edit
        return edit

    def _build_page_nav_button(
        self,
        object_name: str,
        icon_name: str,
        tooltip: str,
        on_clicked: Callable[[], None],
    ) -> QToolButton:
        """翻页箭头 — tinted SVG 图标按钮。

        2026-11-22 修:原 `QPushButton("◀"/"▶")` 依赖字体里的实心三角 glyph,
        部分平台显示成方块 / 大小不一。改用 Lucide chevron SVG + 与行尾图标
        同色 tint,跨字体/平台一致,且 QToolButton 原生支持 disabled 态。
        """
        btn = QToolButton()
        btn.setObjectName(object_name)
        btn.setIcon(tinted_action_icon(icon_name, _PAGE_NAV_TINT))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedSize(28, 28)
        btn.setToolTip(self.tr(tooltip))
        btn.clicked.connect(on_clicked)
        return btn

    def _build_prev_button(self) -> QToolButton:
        """上一页箭头(图标按钮,tooltip/信号同旧版)。"""
        btn = self._build_page_nav_button(
            "mediaPagePrev", "page_prev", "上一页", self._on_page_prev
        )
        self.btn_prev = btn
        return btn

    def _build_page_label(self) -> QLabel:
        """2026-08-25 v1.3.0 PR #6:分页指示 label,初值 "1 / 1"。"""
        lbl = QLabel("1 / 1")
        lbl.setMinimumWidth(60)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_page = lbl
        return lbl

    def _build_next_button(self) -> QToolButton:
        """下一页箭头(图标按钮,tooltip/信号同旧版)。"""
        btn = self._build_page_nav_button(
            "mediaPageNext", "page_next", "下一页", self._on_page_next
        )
        self.btn_next = btn
        return btn

    def _build_refresh_button(self) -> QPushButton:
        """刷新按钮(文案走 tr() 包装)。"""
        btn = QPushButton(self.tr("刷新"))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(self.tr("重新载入媒体列表(F5)"))
        btn.clicked.connect(self.refresh_requested.emit)
        self.btn_refresh = btn
        return btn

    def _build_toolbar(self) -> QVBoxLayout:
        """[Select All] [Retry Selected] [Delete Selected] [Clear Channel] [Prune Orphans] + status label。"""
        vbox = QVBoxLayout()
        vbox.setSpacing(6)
        vbox.addLayout(self._build_action_buttons())
        vbox.addWidget(self._build_status_label())
        return vbox

    def _build_action_buttons(self) -> QHBoxLayout:
        """底部 action 行 — 8 个按钮 + 1 个 checkbox + 1 个 stretch + prune。"""
        actions = QHBoxLayout()
        actions.setSpacing(8)

        # 工具栏按钮(文案走 tr() 包装)
        self.btn_select_all = QPushButton(self.tr("全选"))
        self.btn_select_all.setCursor(Qt.PointingHandCursor)
        self.btn_select_all.clicked.connect(self._on_select_all)
        actions.addWidget(self.btn_select_all)

        self.btn_retry_sel = QPushButton(self.tr("重试所选"))
        self.btn_retry_sel.setCursor(Qt.PointingHandCursor)
        self.btn_retry_sel.setEnabled(False)
        self.btn_retry_sel.clicked.connect(self._on_batch_retry)
        actions.addWidget(self.btn_retry_sel)

        self.btn_delete_sel = QPushButton(self.tr("删除所选"))
        self.btn_delete_sel.setCursor(Qt.PointingHandCursor)
        self.btn_delete_sel.setEnabled(False)
        self.btn_delete_sel.clicked.connect(self._on_batch_delete)
        actions.addWidget(self.btn_delete_sel)

        # 2026-11-22 用户选型:批量下载选中的未下载(仅未下载可下)
        self.btn_download_sel = QPushButton(self.tr("下载所选"))
        self.btn_download_sel.setCursor(Qt.PointingHandCursor)
        self.btn_download_sel.setEnabled(False)
        self.btn_download_sel.setToolTip(self.tr("下载选中行里尚未下载的媒体原文件"))
        self.btn_download_sel.clicked.connect(self._on_batch_download)
        actions.addWidget(self.btn_download_sel)

        # 2026-08-25 PR #4:按频道批量删除 — 清空 filter 选中频道的全部 message
        # (含 media + bytes);MainWindow 接到信号二次确认后再调 VM。
        self.btn_clear_channel = QPushButton(self.tr("清空频道"))
        self.btn_clear_channel.setCursor(Qt.PointingHandCursor)
        self.btn_clear_channel.setToolTip(
            self.tr("删除该频道全部消息(不可撤销)"),
        )
        self.btn_clear_channel.clicked.connect(self._on_clear_channel)
        actions.addWidget(self.btn_clear_channel)

        # 2026-08-25 v1.3.0 PR #7:Media Manager 当前视图 → CSV 一键导出
        # (filter / sort / 全部页,不只是当前页)。MainWindow 接信号 → 构造
        # MediaExportRequest → 调 vm.export_media_list。
        self.btn_export_csv = QPushButton(self.tr("导出 CSV"))
        self.btn_export_csv.setCursor(Qt.PointingHandCursor)
        self.btn_export_csv.setToolTip(
            self.tr("导出当前筛选/排序视图到 CSV(全部页)"),
        )
        self.btn_export_csv.clicked.connect(self._on_export_csv)
        actions.addWidget(self.btn_export_csv)

        # 2026-09-01 v1.5.1 PR #B4:ZIP 打包导出 — 镜像 CSV 流程,emit
        # `export_zip_requested(out_path, include_thumbnails)`。缩略图勾
        # 选用 widget 内的 `chk_zip_thumbs` 开关;MainWindow 接到后构造
        # `ExportRequest(format=ZIP, include_thumbnails=...)`。
        self.btn_export_zip = QPushButton(self.tr("导出 ZIP"))
        self.btn_export_zip.setCursor(Qt.PointingHandCursor)
        self.btn_export_zip.setToolTip(
            self.tr("把当前筛选视图的媒体 bytes + manifest.json 打包成 .zip"),
        )
        self.btn_export_zip.clicked.connect(self._on_export_zip)
        actions.addWidget(self.btn_export_zip)
        # 缩略图打包开关 — 默认 unchecked(纯媒体包);只在 ZIP 按钮旁,
        # 不影响 CSV 流程。
        self.chk_zip_thumbs = QCheckBox(self.tr("含缩略图"))
        self.chk_zip_thumbs.setCursor(Qt.PointingHandCursor)
        self.chk_zip_thumbs.setToolTip(
            self.tr("打包 ZIP 时,同时拉每条媒体 thumb_key 写入 thumb_<arcname>"),
        )
        actions.addWidget(self.chk_zip_thumbs)

        # 2026-11-22 用户选型:一键下完当前筛选视图的全部未下载
        self.btn_download_all = QPushButton(self.tr("下载全部待下"))
        self.btn_download_all.setCursor(Qt.PointingHandCursor)
        self.btn_download_all.setToolTip(self.tr("下载当前筛选视图里全部未下载的媒体原文件"))
        self.btn_download_all.clicked.connect(self.download_all_pending_requested.emit)
        actions.addWidget(self.btn_download_all)

        actions.addStretch(1)

        self.btn_prune = QPushButton(self.tr("清理孤儿"))
        self.btn_prune.setCursor(Qt.PointingHandCursor)
        self.btn_prune.setToolTip(self.tr("扫描 ObjectStore vs storage 并删除孤儿 bytes(不可撤销)"))
        self.btn_prune.clicked.connect(self.prune_requested.emit)
        actions.addWidget(self.btn_prune)

        return actions

    def _build_status_label(self) -> QLabel:
        """底部 status label — 渲染统计(_render_list 更新)+ on_channel_cleared 反馈。"""
        lbl = QLabel(self.tr("加载中…"))
        lbl.setObjectName("mediaManagerStatus")
        lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.lbl_status = lbl
        return lbl

    # ---- 文案访问器(供 mixin 侧取字串)----

    # ---- FilterMixin 用 ----

    def _tr_all_channels(self) -> str:
        """频道下拉的「全部」占位 — FilterMixin 重建下拉时复用同一中文串。"""
        return self.tr("全部频道")

    # ---- ActionsMixin 用(10-γ)----

    def _tr_export_csv_title(self) -> str:
        """CSV 导出对话框标题。"""
        return self.tr("导出 Media Manager 当前视图")

    def _tr_export_csv_filter(self) -> str:
        """CSV 导出对话框 filter 串。"""
        return self.tr("CSV 文件 (*.csv)")

    def _tr_export_zip_title(self) -> str:
        """ZIP 导出对话框标题。"""
        return self.tr("导出 Media Manager 当前视图为 ZIP")

    def _tr_export_zip_filter(self) -> str:
        """ZIP 导出对话框 filter 串。"""
        return self.tr("ZIP 文件 (*.zip)")

    def _tr_channel_cleared(self) -> str:
        """清空频道后的 status bar 文案(占位符 `{cid}` / `{n}`)。"""
        return self.tr("已清空频道 #{cid}: 删除 {n} 条消息")

    # ---- RenderMixin 用(10-δ)----

    def _tr_prune_scan_fmt(self) -> str:
        """prune 完成后刷新的 tooltip 模板(占位符 `{backend}`)。

        与 toolbar 初始态的 `扫描 ObjectStore vs storage ...`(无占位)是
        **两条不同翻译** —— 旧的留在 `_build_action_buttons` 里不动。
        """
        return self.tr("扫描 ObjectStore ({backend}) vs storage 并删除孤儿 bytes(不可撤销)")

    def _tr_status_row(self) -> str:
        """列表 footer 统计文案(占位符 `{total}` / `{done}` / `{failed}` / `{size}`)。"""
        return self.tr("{total} 条媒体 · {done} 完成 · {failed} 失败 · 总大小 {size}")

    def _tr_click_preview(self) -> str:
        """可点击缩略图的 tooltip。"""
        return self.tr("点击查看大图")

    def _tr_error_fmt(self) -> str:
        """FAILED 状态列 tooltip 模板(占位符 `{err}`)。"""
        return self.tr("错误: {err}")

    def _tr_pending_hint(self) -> str:
        """未下载状态列 tooltip — 说明这是默认档的正常状态,可手动下载。"""
        return self.tr("未下载原文件(默认只存元数据);点行尾下载图标或批量下载")

    def _tr_downloading_hint(self) -> str:
        """DOWNLOADING 状态列 tooltip — 与「未下载」明确区分。"""
        return self.tr("正在下载原文件…")
