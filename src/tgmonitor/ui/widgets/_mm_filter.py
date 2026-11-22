# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""FilterMixin — 筛选联动 / 分页导航 / filter 导出 — 2026-10-20 plan-batch10-β。

`MediaManagerWidget` 拆 3 mixin + facade 的**第二步**:抽筛选与分页域。

- 6 method:`_wire_filter` / `_render_page_info` / `_on_page_prev` /
  `_on_page_next` / `set_monitored_channels` / `current_filters`
- 持有 widget 引用(均由 facade `_build_*` 建好后挂到 facade):
  `cmb_channel` / `cmb_type` / `cmb_status` / `cmb_sort` / `cmb_dir` /
  `edit_search` / `lbl_page` / `btn_prev` / `btn_next`
- 持有状态:`_page` / `_page_size` / `_total` / `_monitored_channels`
  / `_search_timer`(后者由 `_wire_filter` 懒建)

**本 mixin 不含 UI 文案调用** —— 选它做第一个搬家的 mixin 正是为了
把「QWidget + plain mixin」模式的风险与文案依赖解耦。

MRO:`class MediaManagerWidget(FilterMixin, RenderMixin, ActionsMixin, QWidget)`
—— `QWidget` 在末尾(Shiboken 要求),本 mixin 是 plain class(隐式 `object`),
照抄 `widgets/message_view_api_mixin.py:44` 的既有模式。本 mixin **不写
`__init__`**:11 个实例字段全部由 facade `__init__` 单点赋值(沿用
`viewmodels/_base.py:94-103` 的单点 init 惯例 —— 类体写 mutable default
会被多实例共享,plan-batch9-η 为此红过 3 个 test)。

跨 mixin / 跨 facade 访问靠 Shiboken metaclass 的属性查找聚合,mypy 由
文件头 `disable-error-code="attr-defined,has-type,unused-ignore"` 屏蔽。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tgmonitor.core.dto import SortDir, SortKey
from tgmonitor.ui.widgets._mm_base import _channel_name

if TYPE_CHECKING:
    # 2026-10-20 plan-batch10-β:让 mypy 视 FilterMixin 为 QWidget 间接子类,
    # 否则 `QTimer(self)` 报 arg-type(mypy 只看到 plain object)。运行时靠
    # facade 的多继承(MRO 末 `QWidget`)兜底 —— 不在类体显式继承 QWidget,
    # 避免 Qt 多继承下重复初始化。
    from PySide6.QtWidgets import QWidget

    from tgmonitor.core.dto import ChannelDTO

    _Base = QWidget
else:
    _Base = object


class FilterMixin(_Base):
    """筛选联动 + 分页导航 + filter 导出 — 2026-10-20 plan-batch10-β。

    公开 API 1:1 保留原 `MediaManagerWidget` 筛选域:
    - 2 public method:`set_monitored_channels` / `current_filters`
    - 4 private method:`_wire_filter` / `_render_page_info` /
      `_on_page_prev` / `_on_page_next`
    """

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
        """更新 `lbl_page` 文字 + btn_prev/btn_next enabled 状态。

        2026-11-22 修:总数变小导致当前页越界时,钳制后必须先 emit refresh
        用修正后的 offset 重拉,否则本页 rows 属于旧 offset,列表会空白到
        用户手动刷新。钳制只发生在此分支,不会与 `_on_page_prev/next` 的
        主动 refresh 叠加。
        """
        total_pages = (
            max(1, (self._total + self._page_size - 1) // self._page_size) if self._total else 1
        )
        # 当前 page 不能超 total_pages(极端 case:total 变小后,_page 还在旧 max)
        clamped = self._page >= total_pages
        if clamped:
            self._page = max(0, total_pages - 1)
        self.lbl_page.setText(f"{self._page + 1} / {total_pages}")
        self.btn_prev.setEnabled(self._page > 0)
        self.btn_next.setEnabled(self._page + 1 < total_pages)
        if clamped:
            self.refresh_requested.emit()

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

    def set_monitored_channels(self, channels: list[ChannelDTO]) -> None:
        """VM.bootstrap_ui → MainWindow → 这里。

        重新填充 channel combo;保留当前选中。
        """
        cur = self.cmb_channel.currentData()
        self._monitored_channels = {c.id: c for c in channels}
        self.cmb_channel.blockSignals(True)
        self.cmb_channel.clear()
        # 2026-11-22 修:原硬编码 "All channels" 在 VM 推频道列表后把中文占位
        # 冲掉;统一走 facade 文案访问器。下拉项 title 优先(不再只显 @username)。
        self.cmb_channel.addItem(self._tr_all_channels(), None)
        # 按频道名排序
        for c in sorted(channels, key=lambda x: _channel_name(x).lower()):
            self.cmb_channel.addItem(_channel_name(c), c.id)
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
        sort = self.cmb_sort.currentData()
        sort_dir = self.cmb_dir.currentData()
        return {
            "channel_id": self.cmb_channel.currentData(),
            "media_type": self.cmb_type.currentData(),
            "status": self.cmb_status.currentData(),
            "search": self.edit_search.text().strip(),
            "sort": sort if isinstance(sort, SortKey) else SortKey.DATE,
            "sort_dir": sort_dir if isinstance(sort_dir, SortDir) else SortDir.DESC,
            "offset": self._page * self._page_size,
            # 2026-11-22 修:UI 每页 50,VM 默认 limit=1000 —— 不显式透传
            # limit 会让 page N/M 的 M 按 50 算、实际一页拉 1000 条。
            "limit": self._page_size,
            "total": self._total,
        }
