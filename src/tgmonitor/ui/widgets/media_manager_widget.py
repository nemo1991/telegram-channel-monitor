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

i18n 硬约束(2026-10-20 plan-batch10 实证,**改代码前必读**):
- 本类 38 条翻译挂在 `.ts` 的 `MediaManagerWidget` context 下,`en_US.ts` /
  `zh_CN.ts` 的 `<location>` 全部指向本文件。
- **lupdate 静态扫描按「enclosing class name」定 context,不认
  `QCoreApplication.translate(ctx, src)` 的第一个参数。** 反证:
  `status_bar.py` 11 处双字面量 `translate("status_bar", "…")`,
  `.ts` 历史里从未出现过 `status_bar` context;`nav_bar` / `state_labels`
  用变量 src,context 列表里同样没有这两个名字。
- 因此**本文件里所有 `self.tr("字面量")` 必须物理留在 `MediaManagerWidget`
  类体内**。挪进 `_mm_render.RenderMixin` 等 mixin,下次有人跑
  `lupdate -extensions py` 时这 38 条会被标 `type="vanished"` →
  `lrelease` 产出空 `.qm` → **i18n 全炸**(2026-09-07 已踩过,见
  CHANGELOG 「pyside6-lupdate 默认 extensions 不含 .py」事故)。
- 需要给 mixin 供文案时,走 facade 里的 `_tr_xxx()` accessor(字面量仍写
  在本类体内,mixin 侧只调 accessor 取已译字串),不要在 mixin 里直接写
  字面量。house rule 同 `widgets/form_section.py` 「caller 提供已 tr()
  过的字串」。
- 守门:`tests/test_media_manager_i18n.py` 双向校验「源码字面量 ↔ .ts source」。
"""

# mypy: disable-error-code="attr-defined"

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, Qt, Signal
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


# 2026-10-20 plan-batch10-α:`_RowKey` / `_format_size` / `_MEDIA_ICONS` /
# `_STATUS_TEXT` / `LIGHTBOX_PREVIEWABLE_TYPES` 已抽到 `widgets/_mm_base.py`
# —— 被 facade + 3 个 mixin 共用,留在本文件会造成 mixin → facade → mixin
# 循环 import。`_MEDIA_ICONS` / `_STATUS_TEXT` / `_format_size` 只被 mixin 用,
# facade 不再 import;`LIGHTBOX_PREVIEWABLE_TYPES` 见上方反向 re-export。

# ---------- 主 widget ----------


class MediaManagerWidget(FilterMixin, RenderMixin, ActionsMixin, QWidget):
    """Media Manager 主页:筛选 + 列表 + 工具栏。

    2026-10-20 plan-batch10 起本类是 **facade**(1149 → 523 LOC)。运行时
    行为拆到 3 个 mixin,本类只留「对外契约 + 静态装配 + i18n 真理源」。
    `QWidget` 必须在 MRO 末尾(Shiboken 要求)。

    ## 分布索引(改代码前先看这里,别在 facade 里找运行逻辑)

    | 归属 | 文件 | 内容 |
    |---|---|---|
    | **facade(本类)** | `media_manager_widget.py` | 14 Signal(对外契约,`main_window.py` 按名 connect)+ `__init__` 单点 init(10 个实例字段)+ `changeEvent` / `retranslateUi`(test 用 `__dict__` 断言强制留本类)+ 15 个静态 chrome `_build_*`(再建 20 个 widget 引用)+ **9 个 `_tr_*()` i18n 字面量真理源** |
    | `FilterMixin` | `_mm_filter.py` | 6 method — 筛选联动(300ms debounce)/ 分页导航 / `current_filters` / `set_known_channels` |
    | `RenderMixin` | `_mm_render.py` | 16 method — VM 注入 / 缩略图 LRU / 列表重渲 / 行构建 / meta 列 / 行内按钮 / 下载进度 |
    | `ActionsMixin` | `_mm_actions.py` | 11 method — 选中集 / 批量重试·删除·清频道 / CSV·ZIP 导出 |
    | 共享常量 | `_mm_base.py` | `_RowKey` / `_format_size` / `_MEDIA_ICONS` / `_STATUS_TEXT` / `LIGHTBOX_PREVIEWABLE_TYPES` |

    ## 三条硬约束

    1. **Signal 全留 facade**(偏离 plan-batch9 的「Signal 下沉 mixin」):
       14 个全被 `main_window` connect,是对外公开契约,留本类让 API 一眼可见。
       Mixin 侧只 `self.<signal>.emit(...)`,不定义也不改 Signal。
    2. **i18n 字面量必须留 facade 类体内**:lupdate 按 enclosing class 定
       context,挪进 mixin 会让 `.ts` 的 38 条变 `vanished`(CHANGELOG:1474)。
       Mixin 侧一律调 `self._tr_xxx()` 取已翻译字串。
    3. **单点 init**:10 个实例字段全在 `__init__` 赋值,mixin **不写**
       `__init__`、类体**禁止** mutable default(`= {}` / `= []` 会跨实例共享,
       plan-batch9-η 为此红过 3 个 test)。
    """

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
        # 2026-09-01 v1.5.1 PR #B3:同模式 status 列 label — 进度事件
        # 进来时按 `(channel_id, telegram_msg_id, media_idx)` 定位 row
        # 然后 setText 刷新「已下载 X / Y (Z%)」文字。重渲时清空重建。
        self._status_labels: dict[_RowKey, QLabel] = {}

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

    # ---- 2026-09-11 v1.7.5:LanguageChange 触发 retranslateUi ----

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 — Qt override
        """Qt 在 LanguageChange 事件 → 重调 retranslateUi 刷新所有文案。"""
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()

    def retranslateUi(self) -> None:  # noqa: N802 — Qt naming
        """2026-09-11 v1.7.5:语言切换后重建所有动态 tr() 文案。

        直接重设 `self.cmb_*` 的 item(避免触发 currentIndexChanged),toolbar 按钮
        text/tooltip,status label 重翻译。筛选条件(value)不变 — user 不感知。

        设计:combo 的「全部频道/全部类型/全部状态」index 0 是 None 占位,
        不能简单 clear+repopulate(会触发信号 + 漏掉占位),改用 setItemText
        精准改 index 0 文字。
        """
        # filter combo index 0 placeholder
        self.cmb_channel.setItemText(0, self.tr("全部频道"))
        self.cmb_type.setItemText(0, self.tr("全部类型"))
        self.cmb_status.setItemText(0, self.tr("全部状态"))
        # sort key labels(index 0..N 一一对应 SortKey 枚举顺序)
        sort_keys = list(SortKey)
        for i, sk in enumerate(sort_keys, start=0):
            label = {
                "date": self.tr("日期"),
                "size": self.tr("大小"),
                "status": self.tr("状态"),
            }[sk.value]
            self.cmb_sort.setItemText(i, label)
        # dir
        self.cmb_dir.setItemText(0, self.tr("↓ 降序"))
        self.cmb_dir.setItemText(1, self.tr("↑ 升序"))
        # search placeholder
        self.edit_search.setPlaceholderText(self.tr("搜索文件名…"))
        # pagination + refresh
        self.btn_prev.setToolTip(self.tr("上一页"))
        self.btn_next.setToolTip(self.tr("下一页"))
        self.btn_refresh.setText(self.tr("🔄 刷新"))
        self.btn_refresh.setToolTip(self.tr("重新载入媒体列表(F5)"))
        # toolbar 操作
        self.btn_select_all.setText(self.tr("全选"))
        self.btn_retry_sel.setText(self.tr("重试所选"))
        self.btn_delete_sel.setText(self.tr("删除所选"))
        self.btn_clear_channel.setText(self.tr("🗑 清空频道"))
        self.btn_clear_channel.setToolTip(self.tr("删除该频道全部消息(不可撤销)"))
        self.btn_export_csv.setText(self.tr("📤 导出 CSV"))
        self.btn_export_csv.setToolTip(self.tr("导出当前筛选/排序视图到 CSV(全部页)"))
        self.btn_export_zip.setText(self.tr("📦 导出 ZIP"))
        self.btn_export_zip.setToolTip(
            self.tr("把当前筛选视图的媒体 bytes + manifest.json 打包成 .zip"),
        )
        self.btn_prune.setText(self.tr("🧹 清理孤儿"))
        # chk_zip_thumbs:label 已建时 tr 过,重建得走一次 tr 刷新
        self.chk_zip_thumbs.setText(self.tr("含缩略图"))

    def _build_filter_bar(self) -> QHBoxLayout:
        """[Channel ▼] [Type ▼] [Status ▼] [Sort ▼] [Dir ▼] [Search🔍] [◀ Page N/M ▶] [Refresh]。"""
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
        # 2026-09-11 v1.7.5:Media Manager 整页 i18n — 走 self.tr() 让 en_US / zh_CN 双语都覆盖
        hbox.addWidget(self._build_refresh_button())
        return hbox

    def _build_channel_filter(self) -> QComboBox:
        """顶部 Channel ▼ — index 0 = None 占位('All Channels')。"""
        cmb = QComboBox()
        cmb.setMinimumWidth(160)
        cmb.addItem(self.tr("全部频道"), None)
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
        """顶部 Status ▼ — index 0 = None 占位,后续枚举所有 MediaDownloadStatus。"""
        cmb = QComboBox()
        cmb.setMinimumWidth(120)
        cmb.addItem(self.tr("全部状态"), None)
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

    def _build_prev_button(self) -> QPushButton:
        """2026-08-25 v1.3.0 PR #6:分页上一页按钮。"""
        btn = QPushButton("◀")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedWidth(32)
        btn.setToolTip(self.tr("上一页"))
        btn.clicked.connect(self._on_page_prev)
        self.btn_prev = btn
        return btn

    def _build_page_label(self) -> QLabel:
        """2026-08-25 v1.3.0 PR #6:分页指示 label,初值 "1 / 1"。"""
        lbl = QLabel("1 / 1")
        lbl.setMinimumWidth(60)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_page = lbl
        return lbl

    def _build_next_button(self) -> QPushButton:
        """2026-08-25 v1.3.0 PR #6:分页下一页按钮。"""
        btn = QPushButton("▶")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedWidth(32)
        btn.setToolTip(self.tr("下一页"))
        btn.clicked.connect(self._on_page_next)
        self.btn_next = btn
        return btn

    def _build_refresh_button(self) -> QPushButton:
        """2026-09-11 v1.7.5:刷新按钮 — 走 self.tr() 让 en_US / zh_CN 双语都覆盖。"""
        btn = QPushButton(self.tr("🔄 刷新"))
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

        # 2026-09-11 v1.7.5:Media Manager 整页 i18n — 走 self.tr() 让 en_US / zh_CN 双语覆盖
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

        # 2026-08-25 PR #4:按频道批量删除 — 清空 filter 选中频道的全部 message
        # (含 media + bytes);MainWindow 接到信号二次确认后再调 VM。
        self.btn_clear_channel = QPushButton(self.tr("🗑 清空频道"))
        self.btn_clear_channel.setCursor(Qt.PointingHandCursor)
        self.btn_clear_channel.setToolTip(
            self.tr("删除该频道全部消息(不可撤销)"),
        )
        self.btn_clear_channel.clicked.connect(self._on_clear_channel)
        actions.addWidget(self.btn_clear_channel)

        # 2026-08-25 v1.3.0 PR #7:Media Manager 当前视图 → CSV 一键导出
        # (filter / sort / 全部页,不只是当前页)。MainWindow 接信号 → 构造
        # MediaExportRequest → 调 vm.export_media_list。
        self.btn_export_csv = QPushButton(self.tr("📤 导出 CSV"))
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
        self.btn_export_zip = QPushButton(self.tr("📦 导出 ZIP"))
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

        actions.addStretch(1)

        self.btn_prune = QPushButton(self.tr("🧹 清理孤儿"))
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

    # ---- i18n 字面量真理源(供 mixin 侧取「已翻译」字串)----
    # 2026-10-20 plan-batch10-γ/δ:被搬进 `_mm_actions.ActionsMixin` /
    # `_mm_render.RenderMixin` 的 9 处 tr 字面量必须物理留在本类体内 ——
    # lupdate 静态扫描按 **enclosing class name** 定 context,挪进 mixin 会让
    # `.ts` 的 38 条 MediaManagerWidget 翻译在下次 lupdate 时被标
    # type="vanished" → lrelease 产出空 .qm → i18n 全炸(CHANGELOG:1474 已踩过)。
    # Mixin 侧只调本组 accessor 取**已翻译**字串,遵循 form_section.py:31-35
    # 「caller 提供已 tr() 过的字串,FormSection 不二次 tr」house rule。
    # 守门见 tests/test_media_manager_i18n.py::test_all_widget_tr_literals_exist_in_ts。

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
