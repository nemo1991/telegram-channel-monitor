# mypy: disable-error-code="attr-defined"
"""DashboardWidget — 大盘页:只读统计卡 + 事件时间线 + 频道统计表。

2026-11-17 v2.0.3 D4/D6:
- 快速操作行(刷新 / 导出 / 全量同步)整体移除,大盘回归纯只读看板。
  刷新与全量同步在频道页,消息导出 UI 入口按用户确认移除(core 保留)。
- 「最近活动」不再逐条显示 `MessageReceived` 消息文本,其余 6 类事件保留。
- 「已加入频道」取库中 `tg_subscribed=1` 计数,与「已监听」互不等价。
- 「频道统计」显示真实 title / 中文 kind,缺数据留空,不再恒显 `#id / —`。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.activity import format_activity
from tgmonitor.core.events import (
    BatchDone,
    ChannelMonitored,
    ChannelSyncDone,
    ChannelUnmonitored,
    ErrorOccurred,
    ExportDone,
    JoinedChannelsRefreshed,
    LoginStateChanged,
    MediaReconcileFinished,
    SettingsChanged,
)
from tgmonitor.ui.async_bridge import run_coro
from tgmonitor.ui.icon import action_icon
from tgmonitor.ui.state_labels import state_badge, state_color_bucket

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.dto import ChannelDTO
    from tgmonitor.core.events import EventBus
    from tgmonitor.core.monitor.service import MonitorService
    from tgmonitor.ui.widgets.message_detail import MessageDetail
    from tgmonitor.ui.widgets.message_view import MessageView

import asyncio

log = logging.getLogger(__name__)

# 2026-11-17 v2.0.3 D6:频道 kind → 中文类型。未知 / 空值留空(不写恒定的「—」)。
_KIND_LABELS: dict[str, str] = {
    "channel": "频道",
    "supergroup": "群组",
    "basic_group": "群组",
}


class _StatCard(QFrame):
    """单个统计卡片 — 一行内展示「图标 + 标题 + 数值」。

    QSS 用 objectName="statCard" 统一样式(白底圆角 + 阴影)。
    2026-11-19 v2.0.7 FR-01:两行(标题行 + 数值行)合并为单行,高度 68→48。
    存储后端卡用 `set_value_pair()` 把 DB / 对象并排展示,二者同为
    #statValue 字号;FR-02 给数值 label 挂 `property("state", 档位)`,
    色号真值源在 style.qss。
    """

    def __init__(self, title: str, icon_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statCard")
        # v2.0.5 FR-02:100→68;v2.0.7 FR-01 单行化后再压到 48。
        self.setFixedHeight(48)
        self.setMinimumWidth(160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 6, 14, 6)
        row.setSpacing(8)

        ico = QLabel()
        ico.setPixmap(action_icon(icon_name).pixmap(16, 16))
        ico.setFixedSize(16, 16)
        row.addWidget(ico)
        title_lbl = QLabel(title)
        title_lbl.setProperty("role", "card-title")
        row.addWidget(title_lbl)
        row.addStretch(1)

        # 数值与标题同排:单值 or 两个等值(存储后端)。
        self.value_lbl = QLabel("—")
        self.value_lbl.setObjectName("statValue")
        row.addWidget(self.value_lbl)
        self.value2_lbl = QLabel("")
        self.value2_lbl.setObjectName("statValue")
        self.value2_lbl.hide()  # 默认单值,双值卡按需 show
        row.addWidget(self.value2_lbl)

    def set_value(self, value: str) -> None:
        """设置单个数值(默认形态),隐藏第二数值。"""
        self.value_lbl.setText(value)
        self.value2_lbl.hide()

    def set_value_pair(self, value: str, value2: str) -> None:
        """并排显示两个**等大**数值(存储后端卡:DB + 对象)。"""
        self.value_lbl.setText(value)
        self.value2_lbl.setText(value2)
        self.value2_lbl.show()

    def set_state(self, state: str) -> None:
        """按登录/连接状态设置数值 label 的语义色档位(色号由 QSS 定义)。"""
        bucket = state_color_bucket(state)
        if self.value_lbl.property("state") == bucket:
            return
        self.value_lbl.setProperty("state", bucket)
        # Qt 在动态 property 变化后需重新 polish 才会应用新的 QSS 规则。
        self.value_lbl.style().unpolish(self.value_lbl)
        self.value_lbl.style().polish(self.value_lbl)


class _ActivityEntry(QWidget):
    """时间线一条记录:时间 + 图标 + 文本。

    2026-11-17 v2.0.4 FR-01:行高必须按**实际可用宽度**算。原来的 bug 是
    在宽度未定时取 `sizeHint()`,而 `msg_lbl` 开了 wordWrap —— Qt 在未定宽时
    按单行算高度,列表又把该高度锁死;真实渲染时文本换成多行,垂直居中后
    上下被裁。这里改为:entry 记录自己的文本,由 `height_for_width()` 用
    QFontMetrics 按给定宽度算换行高度,再由列表用同一宽度 setSizeHint。
    """

    # 水平固定占用:时间标签 60 + 图标 14 + 两个 spacing(8×2) = 90px。
    _H_FIXED = 90
    # 垂直余量:hbox 上下 margin(2×2) + QSS item padding(4×2) + 1px 容差。
    _V_PAD = 13

    def __init__(
        self,
        icon_name: str,
        text: str,
        occurred_at: datetime | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._text = text
        hbox = QHBoxLayout(self)
        hbox.setContentsMargins(0, 2, 0, 2)
        hbox.setSpacing(8)

        # FR-01:时间取事件发生时刻(由 Event.occurred_at 注入),不再用渲染时刻。
        # FR-05:库里存 UTC aware datetime,展示按本地时区。
        when = occurred_at or datetime.now()
        if when.tzinfo is not None:
            when = when.astimezone()
        time_lbl = QLabel(when.strftime("%H:%M:%S"))
        time_lbl.setFixedWidth(60)
        time_lbl.setProperty("role", "hint")
        hbox.addWidget(time_lbl)

        ico = QLabel()
        ico.setPixmap(action_icon(icon_name).pixmap(14, 14))
        ico.setFixedSize(14, 14)
        hbox.addWidget(ico)

        self.msg_lbl = QLabel(text)
        self.msg_lbl.setWordWrap(True)
        hbox.addWidget(self.msg_lbl, 1)

    def height_for_width(self, total_width: int) -> int:
        """按列表可用宽度算该条需要的真实高度(用于 item.setSizeHint)。"""
        text_width = max(40, total_width - self._H_FIXED - 20)  # 20 = QSS 左右 padding
        fm = QFontMetrics(self.msg_lbl.font())
        rect = fm.boundingRect(
            0, 0, text_width, 100_000, Qt.TextWordWrap | Qt.AlignLeft, self._text
        )
        return rect.height() + self._V_PAD


class DashboardWidget(QWidget):
    """大盘(Ops Dashboard) —— MainWindow 作为内容页插入 QStackedWidget。

    通过 `update_stats()` 由 MainWindow._refresh_state 调用刷新。
    EventBus 事件 → 活动时间线自动追加。
    """

    def __init__(
        self,
        app: AppService,
        monitor: MonitorService,
        loop: asyncio.AbstractEventLoop | None = None,
        channels_provider: Callable[[], Mapping[int, ChannelDTO]] | None = None,
        live_view: MessageView | None = None,
        message_detail: MessageDetail | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """建 5 张统计卡 + 实时流 + 时间线 + 频道表 + 接 EventBus + 心跳。

        `loop=None` 用于无 loop 的测试环境,会跳过异步取数。
        `channels_provider` 由 MainWindow 注入 VM 的 monitored_channels,供频道
        统计表显示真实 title / kind;未提供时退化为 `#id` + 空类型。
        2026-11-17 v2.0.4 FR-03:`live_view` / `message_detail` 由 MainWindow
        注入(单实例从独立 LIVE 页迁入);未提供时实时流区留空(测试环境)。
        """
        super().__init__(parent)
        self._app = app
        self._monitor = monitor
        self._loop = loop
        self._channels_provider = channels_provider
        self._live_view = live_view
        self._message_detail = message_detail
        self._build()
        self._wire_bus()
        # v2.0.4 FR-05:启动时从库加载最近活动(重启后不再空白)。
        self._load_activities()

        # 定时刷新:每 30s 更新一次时间线(不需要拉新数据,只是 UI 活跃感)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(30_000)
        self._refresh_timer.timeout.connect(self._tick_timeline)
        self._refresh_timer.start()

    # ---------- 装配 ----------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        # v2.0.5 FR-02:收紧页边距与行距(24/20 → 16/12),释放纵向空间。
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # v2.0.5 FR-01:原 "操作台" pageTitle 标题移除,把纵向空间让给消息流。
        # 全局 #pageTitle QSS 规则保留(设置页/详情页等仍依赖)。

        # --- 统计卡片行 ---
        cards = QHBoxLayout()
        cards.setSpacing(10)

        self.card_channels = _StatCard("已加入频道", "nav_channels")
        cards.addWidget(self.card_channels)

        self.card_monitored = _StatCard("已监听", "kind_channel")
        cards.addWidget(self.card_monitored)

        self.card_state = _StatCard("连接状态", "nav_live")
        cards.addWidget(self.card_state)

        self.card_storage = _StatCard("存储后端", "settings")
        cards.addWidget(self.card_storage)

        # 2026-11-19 v2.0.7 FR-01:「消息总数」顶卡删除 —— 与底部「频道统计」
        # 表第三列同源同口径(同一 rows 聚合),信息只保留一次。
        root.addLayout(cards)

        # 2026-11-17 v2.0.3 D4:快速操作行(刷新 / 导出 / 全量同步)整体移除。
        # 大盘回归只读监控看板;等价入口见频道页,消息导出 UI 入口按用户
        # 确认移除(core ExportService / ExportController 保留)。

        # --- 实时流区(v2.0.4 FR-03:从独立 LIVE 页迁入,单实例) ---
        # MessageView + MessageDetail 由 MainWindow 注入并持有引用,这里只
        # 做 reparent + 布局。splitter 比例 2:1(消息流占大头)。
        #
        # 2026-11-18 v2.0.5:外围四角圆角 + 接壤直角。用 QFrame#liveWrap 作
        # 统一容器画白底/边框/圆角;splitter handle(1px)画接壤的竖直直角缝。
        # 关键:QScrollArea 的 viewport 会盖住 frame 的圆角,所以两个子 widget
        # 自身无边框、viewport 背景透明(内联 stylesheet 一定命中),让容器
        # 的白底与圆角显形。splitter 仍是 live_view/message_detail 的直接
        # parent,控制器/测试对 widget 树的依赖不变。
        if self._live_view is not None and self._message_detail is not None:
            live_wrap = QFrame(self)
            live_wrap.setObjectName("liveWrap")
            wrap_layout = QVBoxLayout(live_wrap)
            wrap_layout.setContentsMargins(0, 0, 0, 0)
            wrap_layout.setSpacing(0)

            live_splitter = QSplitter(Qt.Horizontal)
            live_splitter.setObjectName("dashboardLiveSplitter")
            live_splitter.setHandleWidth(1)
            live_splitter.setChildrenCollapsible(False)
            # viewport 透明:让 liveWrap 的白底/圆角显形(见上方说明)。
            self._live_view.viewport().setStyleSheet("QWidget { background: transparent; }")
            self._message_detail.viewport().setStyleSheet("QWidget { background: transparent; }")
            live_splitter.addWidget(self._live_view)
            live_splitter.addWidget(self._message_detail)
            live_splitter.setStretchFactor(0, 2)
            live_splitter.setStretchFactor(1, 1)
            live_splitter.setSizes([800, 400])
            wrap_layout.addWidget(live_splitter)
            root.addWidget(live_wrap, 3)

        # --- 下方左右两块:最近活动 | 频道统计 ---
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(20)

        # 活动时间线(左)
        # v2.0.5 FR-04:dashPanel 专属 QSS 让标题归位框内,不影响全局 QGroupBox。
        tl_group = QGroupBox("最近活动")
        tl_group.setObjectName("dashPanel")
        tl_vbox = QVBoxLayout(tl_group)
        tl_vbox.setContentsMargins(0, 0, 0, 0)
        tl_vbox.setSpacing(0)

        self.activity_list = QListWidget()
        self.activity_list.setFrameShape(QFrame.NoFrame)
        self.activity_list.setAlternatingRowColors(True)
        tl_vbox.addWidget(self.activity_list)
        bottom_row.addWidget(tl_group, 1)

        # 已订阅频道统计表(右)
        stats_group = QGroupBox("频道统计")
        stats_group.setObjectName("dashPanel")
        sg_vbox = QVBoxLayout(stats_group)
        sg_vbox.setContentsMargins(0, 0, 0, 0)
        sg_vbox.setSpacing(0)

        self.stats_table = QTableWidget(0, 3)
        # 2026-11-18 v2.0.5:专属 objectName,让 QSS 只给本表头首末 section
        # 收圆(表头浅灰方角会盖住表格 frame 顶部圆角)。
        self.stats_table.setObjectName("statsTable")
        self.stats_table.setHorizontalHeaderLabels(["频道", "类型", "消息数"])
        self.stats_table.verticalHeader().setVisible(False)
        self.stats_table.setFrameShape(QFrame.NoFrame)
        self.stats_table.setAlternatingRowColors(True)
        self.stats_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.stats_table.setSelectionMode(QTableWidget.NoSelection)
        # 表格 stretch:第一列拉满
        hdr = self.stats_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.stats_table.setMaximumHeight(180)
        sg_vbox.addWidget(self.stats_table)
        bottom_row.addWidget(stats_group, 1)

        root.addLayout(bottom_row, 2)

    # ---------- EventBus ----------

    def _wire_bus(self) -> None:
        bus: EventBus = self._app.bus
        bus.subscribe(LoginStateChanged, self._on_bus_event)
        # 2026-11-17 v2.0.3 D4:不再订阅 MessageReceived —— 最近活动是事件
        # 时间线,不是最近消息列表。其余 6 类事件保留。
        bus.subscribe(ChannelMonitored, self._on_bus_event)
        bus.subscribe(ChannelUnmonitored, self._on_bus_event)
        bus.subscribe(ExportDone, self._on_bus_event)
        bus.subscribe(SettingsChanged, self._on_bus_event)
        bus.subscribe(ErrorOccurred, self._on_bus_event)
        # v2.0.4:新增覆盖类型的实时追加,只需在此加一行 subscribe +
        # core/activity.py 的 _FORMATTERS 加一行(落库与 UI 共用口径)。
        bus.subscribe(BatchDone, self._on_bus_event)
        bus.subscribe(ChannelSyncDone, self._on_bus_event)
        bus.subscribe(MediaReconcileFinished, self._on_bus_event)
        # 2026-11-17 v2.0.5:频道页"刷新已加入频道"成功写库后 → 重读计数卡片。
        # 该数字取自 storage.count_tg_subscribed(),与 VM 的 channels_changed
        # ("监听白名单"语义)不是一回事,刷新走的是 core 写库路径,不会触发
        # MainWindow._refresh_state,所以需要本事件单独驱动大盘重读。
        bus.subscribe(JoinedChannelsRefreshed, self._on_joined_refreshed)

    async def _on_joined_refreshed(self, e: JoinedChannelsRefreshed) -> None:
        """刷新已加入频道成功 → 重读库计数 + 频道统计表。

        显示以**读库为准**(不用 e.count),因为库是 tg_subscribed 的真理,
        且级联退订会在同一轮写库;读一次即可覆盖计数与每频道统计两处。
        """
        self._refresh_joined_count()
        self._refresh_per_channel_stats()

    async def _on_bus_event(self, e) -> None:
        """事件 → 追加到活动时间线。

        2026-11-17 v2.0.4 FR-04:文案统一走 core `format_activity`(与落库
        同一口径),UI 不再维护第二套 `_format_*` 表。登录态事件另需同步
        连接状态卡片,由 `_sync_state_card` 处理。
        """
        if isinstance(e, LoginStateChanged):
            self._sync_state_card(e)
        activity = format_activity(e)
        if activity is not None:
            self._add_activity(
                activity.icon,
                f"{activity.title}: {activity.detail}" if activity.detail else activity.title,
                activity.occurred_at,
            )

    def _sync_state_card(self, e: LoginStateChanged) -> None:
        """登录态事件 → 同步连接状态卡片(纯 UI 副作用,不属于活动文案)。"""
        self.card_state.set_value(state_badge(e.state))
        self.card_state.set_state(e.state)

    def _add_activity(self, icon_name: str, text: str, occurred_at: datetime | None = None) -> None:
        """追加一条活动记录到列表头部(最新的在上面)。

        2026-11-17 v2.0.4 FR-01:sizeHint 用**列表实际可用宽度**算出的
        换行高度,而不是 entry 未定宽时的单行 sizeHint —— 后者会锁死行高
        导致多行文字上下被裁(半宽布局下更明显)。
        """
        available = max(120, self.activity_list.viewport().width())
        entry = _ActivityEntry(icon_name, text, occurred_at)
        item = QListWidgetItem()
        item.setSizeHint(QSize(available, entry.height_for_width(available)))
        self.activity_list.insertItem(0, item)
        self.activity_list.setItemWidget(item, entry)
        # 限制条目数
        while self.activity_list.count() > 200:
            item = self.activity_list.takeItem(self.activity_list.count() - 1)
            if item is not None:
                w = self.activity_list.itemWidget(item)
                if w is not None:
                    w.deleteLater()
                del item

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt override
        """FR-01:列表宽度变化(尤其半宽布局首次布局)时重算所有行高。

        条目可能在列表尚未获得最终宽度时插入;布局稳定后按新宽度重算,
        保证 wordWrap 的真实高度始终被 sizeHint 覆盖,不出现裁切。
        """
        super().resizeEvent(event)
        available = max(120, self.activity_list.viewport().width())
        for i in range(self.activity_list.count()):
            item = self.activity_list.item(i)
            widget = self.activity_list.itemWidget(item) if item is not None else None
            if isinstance(widget, _ActivityEntry):
                item.setSizeHint(QSize(available, widget.height_for_width(available)))

    def _tick_timeline(self) -> None:
        """定时心跳 — 感受 UI 在动就行。"""
        pass

    # ---------- 外部刷新入口 ----------

    def update_stats(self, monitored_count: int) -> None:
        """由 MainWindow._refresh_state 调,刷新统计卡片。

        2026-11-17 v2.0.3 D5/FR-06:「已加入频道」不再拿 VM 的
        `monitored_channels` 凑数,改为异步读库里 `tg_subscribed=1`
        计数;存量库迁移不回填,首次刷新前显示 0 属预期。
        """
        self.card_monitored.set_value(str(monitored_count))

        # 登录状态 — 文案与语义色都走 state_labels 单源映射
        state = self._app.client.state
        self.card_state.set_value(state_badge(state))
        self.card_state.set_state(state)

        # 存储后端 —— v2.0.5:DB 与对象并排、等大(#statValue)。
        settings = self._app.settings
        db_label = settings.db_backend.value if settings.db_backend else "?"
        os_label = settings.objectstore_backend.value if settings.objectstore_backend else "?"
        self.card_storage.set_value_pair(f"DB:{db_label}", f"对象:{os_label}")

        # 异步取数:TG 订阅频道数 + 每频道消息数统计表
        self._refresh_joined_count()
        self._refresh_per_channel_stats()

    # v2.0.4 FR-05:启动从库加载的活动条数(与旧内存上限一致)
    _ACTIVITY_LOAD_LIMIT = 200

    def _load_activities(self) -> None:
        """启动异步加载最近活动(库为空则列表保持空,无副作用)。"""
        if self._loop is None:
            return  # 测试环境无 loop
        run_coro(
            self._loop,
            self._fetch_activities(),
            on_success=self._apply_activities,
            error_label="dashboard_load_activities",
        )

    async def _fetch_activities(self) -> list:
        """从 storage 拉最近 N 条;best-effort,失败按空列表处理。"""
        try:
            return await self._app.storage.list_activities(self._ACTIVITY_LOAD_LIMIT)
        except Exception:  # noqa: BLE001
            log.warning("list_activities failed", exc_info=True)
            return []

    def _apply_activities(self, activities: list) -> None:
        """库历史填充时间线(新的在前),覆盖启动期可能的空列表。"""
        self.activity_list.clear()
        # 库中已是 occurred_at DESC(新的在前);倒序插入使最新显示在列表头。
        for activity in reversed(activities):
            detail = f"{activity.title}: {activity.detail}" if activity.detail else activity.title
            self._add_activity(activity.icon, detail, activity.occurred_at)

    async def _fetch_joined_count(self) -> int:
        """库内 TG 订阅频道数;best-effort,取数失败按 0 处理。"""
        try:
            return await self._app.storage.count_tg_subscribed()
        except Exception:  # noqa: BLE001
            log.warning("count_tg_subscribed failed", exc_info=True)
            return 0

    def _refresh_joined_count(self) -> None:
        if self._loop is None:
            return  # 测试环境无 loop
        run_coro(
            self._loop,
            self._fetch_joined_count(),
            on_success=lambda n: self.card_channels.set_value(str(n)),
            error_label="dashboard_joined_count",
        )

    def _refresh_per_channel_stats(self) -> None:
        """后台拉每频道消息数 → 填表格。

        2026-08-27 v1.4.0 PR #15:N+1 → 单 `aggregate_per_channel` 调用
        替代原来对每个订阅频道串行调 `count_messages`。
        """
        if not self._monitor.monitored_ids:
            self.stats_table.setRowCount(0)
            return
        if self._loop is None:
            return  # 测试环境无 loop

        run_coro(
            self._loop,
            self._fetch_channel_rows(),
            on_success=self._apply_stats_table,
            error_label="dashboard_refresh_stats",
        )

    async def _fetch_channel_rows(self) -> list[tuple[str, str, int]]:
        """单次聚合 + 从 VM 元数据映射取真实 title / kind。

        2026-11-19 v2.0.7 FR-01:原先同时返回 `total` 仅供「消息总数」顶卡,
        该卡删除后 total 成为死计算,一并去掉(表格逐行消息数不变)。
        """
        monitored_ids = list(self._monitor.monitored_ids)
        if not monitored_ids:
            return []
        try:
            bucket = await self._app.storage.aggregate_per_channel(monitored_ids)
        except Exception:  # noqa: BLE001
            log.warning("aggregate_per_channel failed", exc_info=True)
            bucket = {}

        provider = self._channels_provider
        channels: Mapping[int, ChannelDTO] = provider() if provider else {}
        rows: list[tuple[str, str, int]] = []
        for cid in monitored_ids:
            stats = bucket.get(cid)
            # 缺省 → ChannelStats() 全 0
            count = stats.messages if stats else 0
            ch = channels.get(cid)
            title = ch.title if ch is not None and ch.title else f"#{cid}"
            kind = _KIND_LABELS.get(ch.kind, "") if ch is not None else ""
            rows.append((title, kind, count))
        return rows

    def _apply_stats_table(self, rows: list[tuple[str, str, int]]) -> None:
        """从 background loop 调回主线程填充表格。"""
        self.stats_table.setRowCount(len(rows))
        for i, (title, kind, count) in enumerate(rows):
            self.stats_table.setItem(i, 0, QTableWidgetItem(title))
            self.stats_table.setItem(i, 1, QTableWidgetItem(kind))
            count_item = QTableWidgetItem(f"{count:,}")
            count_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.stats_table.setItem(i, 2, count_item)
