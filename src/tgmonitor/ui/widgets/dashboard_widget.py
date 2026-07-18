"""DashboardWidget — 大盘页:统计卡片 + 快速操作 + 活动时间线。

Phase 1 版本:
- 统计卡片:已加入频道数 / 已监听数 / 登录状态 / 消息存储后端
- 快速操作:刷新频道 / 导出 / 全量同步
- 最近活动时间线(滚动日志,EventBus 驱动)

后续 Phase 可扩展:
- 消息量趋势图(按天/频道)
- 存储水位(DB + ObjectStore 用量)
- 速率限制 / 错误统计
- 同步进度概览
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.events import (
    ChannelSubscribed,
    ChannelUnsubscribed,
    ErrorOccurred,
    ExportDone,
    LoginStateChanged,
    MessageReceived,
    SettingsChanged,
)
from tgmonitor.ui.icon import action_icon

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.events import EventBus
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)


class _StatCard(QFrame):
    """单个统计卡片 — 数字 + 标签,纯视觉组件。

    QSS 用 objectName="statCard" 统一样式(白底圆角 + 阴影)。
    """

    def __init__(self, title: str, icon_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statCard")
        self.setFixedHeight(100)
        self.setMinimumWidth(160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(16, 12, 16, 12)
        vbox.setSpacing(4)

        # 图标 + 标题
        top = QHBoxLayout()
        top.setSpacing(8)
        ico = QLabel()
        ico.setPixmap(action_icon(icon_name).pixmap(20, 20))
        ico.setFixedSize(20, 20)
        top.addWidget(ico)
        title_lbl = QLabel(title)
        title_lbl.setProperty("role", "card-title")
        top.addWidget(title_lbl)
        top.addStretch()
        vbox.addLayout(top)

        # 数值
        self.value_lbl = QLabel("—")
        self.value_lbl.setObjectName("statValue")
        vbox.addWidget(self.value_lbl)

        # 副文本
        self.sub_lbl = QLabel("")
        self.sub_lbl.setProperty("role", "hint")
        vbox.addWidget(self.sub_lbl)

    def set_value(self, value: str, sub: str = "") -> None:
        self.value_lbl.setText(value)
        self.sub_lbl.setText(sub)


class _ActivityEntry(QWidget):
    """时间线一条记录:时间 + 图标 + 文本。"""

    def __init__(self, icon_name: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        hbox = QHBoxLayout(self)
        hbox.setContentsMargins(0, 2, 0, 2)
        hbox.setSpacing(8)

        time_lbl = QLabel(datetime.now().strftime("%H:%M:%S"))
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


class DashboardWidget(QWidget):
    """大盘(Ops Dashboard) —— MainWindow 作为内容页插入 QStackedWidget。

    通过 `update_stats()` 由 MainWindow._refresh_state 调用刷新。
    EventBus 事件 → 活动时间线自动追加。
    """

    def __init__(
        self,
        app: AppService,
        monitor: MonitorService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._monitor = monitor
        self._build()
        self._wire_bus()

        # 定时刷新:每 30s 更新一次时间线(不需要拉新数据,只是 UI 活跃感)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(30_000)
        self._refresh_timer.timeout.connect(self._tick_timeline)
        self._refresh_timer.start()

        # 快速操作 callbacks — 由 MainWindow 注入(避免循环 import)
        self.on_refresh: callable | None = None
        self.on_export: callable | None = None
        self.on_sync_all: callable | None = None

    _EVENT_ICONS = {
        LoginStateChanged: "nav_live",
        MessageReceived: "nav_live",
        ChannelSubscribed: "kind_channel",
        ChannelUnsubscribed: "kind_group",
        ExportDone: "export",
        SettingsChanged: "settings",
        ErrorOccurred: "refresh",
    }

    # ---------- 装配 ----------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(20)

        # 标题
        title = QLabel("操作台")
        title.setObjectName("pageTitle")
        root.addWidget(title)

        # --- 统计卡片行 ---
        cards = QHBoxLayout()
        cards.setSpacing(16)

        self.card_channels = _StatCard("已加入频道", "nav_channels")
        cards.addWidget(self.card_channels)

        self.card_subscribed = _StatCard("已监听", "kind_channel")
        cards.addWidget(self.card_subscribed)

        self.card_state = _StatCard("连接状态", "nav_live")
        cards.addWidget(self.card_state)

        self.card_storage = _StatCard("存储后端", "settings")
        cards.addWidget(self.card_storage)

        root.addLayout(cards)

        # --- 快速操作行 ---
        actions = QHBoxLayout()
        actions.setSpacing(12)

        self.btn_refresh = QPushButton(action_icon("refresh"), " 刷新频道")
        self.btn_refresh.setObjectName("actionBtn")
        self.btn_refresh.clicked.connect(lambda: self.on_refresh and self.on_refresh())
        actions.addWidget(self.btn_refresh)

        self.btn_export = QPushButton(action_icon("export"), " 导出…")
        self.btn_export.setObjectName("actionBtn")
        self.btn_export.clicked.connect(lambda: self.on_export and self.on_export())
        actions.addWidget(self.btn_export)

        self.btn_sync = QPushButton(action_icon("nav_channels"), " 全量同步")
        self.btn_sync.setObjectName("actionBtn")
        self.btn_sync.clicked.connect(lambda: self.on_sync_all and self.on_sync_all())
        actions.addWidget(self.btn_sync)

        actions.addStretch()
        root.addLayout(actions)

        # --- 活动时间线 ---
        tl_group = QGroupBox("最近活动")
        tl_vbox = QVBoxLayout(tl_group)
        tl_vbox.setContentsMargins(0, 0, 0, 0)
        tl_vbox.setSpacing(0)

        self.activity_list = QListWidget()
        self.activity_list.setFrameShape(QFrame.NoFrame)
        self.activity_list.setAlternatingRowColors(True)
        tl_vbox.addWidget(self.activity_list)

        root.addWidget(tl_group, 1)

    # ---------- EventBus ----------

    def _wire_bus(self) -> None:
        bus: EventBus = self._app.bus
        bus.subscribe(LoginStateChanged, self._on_bus_event)
        bus.subscribe(MessageReceived, self._on_bus_event)
        bus.subscribe(ChannelSubscribed, self._on_bus_event)
        bus.subscribe(ChannelUnsubscribed, self._on_bus_event)
        bus.subscribe(ExportDone, self._on_bus_event)
        bus.subscribe(SettingsChanged, self._on_bus_event)
        bus.subscribe(ErrorOccurred, self._on_bus_event)

    async def _on_bus_event(self, e) -> None:  # noqa: C901 — 事件类型 switch
        """从 EventBus 收到事件 → 追加到活动时间线。"""
        icon_name = "refresh"
        msg = ""
        if isinstance(e, LoginStateChanged):
            icon_name = "nav_live"
            st = e.state
            detail = f" ({e.detail[:40]})" if e.detail else ""
            msg = f"登录状态切换: {st}{detail}"
            # 同步更新连接状态卡片
            self.card_state.set_value(
                {"ready": "🟢 已登录", "error": "🔴 错误", "phone_required": "🟡 未登录"}.get(st, f"⚪ {st}"),
                e.detail[:60] if e.detail else "",
            )
        elif isinstance(e, MessageReceived):
            if e.message is not None:
                icon_name = "nav_live"
                title = e.message.author or f"#{e.message.channel_id}"
                snippet = (e.message.text or "")[:60]
                msg = f"[{title}] {snippet}"
        elif isinstance(e, ChannelSubscribed):
            icon_name = "kind_channel"
            msg = f"已订阅频道: {e.channel.title if e.channel else '#' + str(id(e))}"
        elif isinstance(e, ChannelUnsubscribed):
            icon_name = "kind_group"
            msg = f"已退订频道: #{e.channel_id}"
        elif isinstance(e, ExportDone):
            icon_name = "export"
            if e.error:
                msg = f"导出失败: {e.error[:60]}"
            else:
                msg = f"导出完成: {e.result.out_path if e.result else '?'}"
        elif isinstance(e, SettingsChanged):
            icon_name = "settings"
            msg = f"设置已变更: {e.what}"
        elif isinstance(e, ErrorOccurred):
            icon_name = "refresh"
            msg = f"⚠ [{e.source}] {e.message[:80]}"

        if msg:
            self._add_activity(icon_name, msg)

    def _add_activity(self, icon_name: str, text: str) -> None:
        """追加一条活动记录到列表头部(最新的在上面)。"""
        entry = _ActivityEntry(icon_name, text)
        item = QListWidgetItem()
        item.setSizeHint(entry.sizeHint())
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

    def _tick_timeline(self) -> None:
        """定时心跳 — 感受 UI 在动就行。"""
        pass

    # ---------- 外部刷新入口 ----------

    def update_stats(self, joined_count: int, subscribed_count: int) -> None:
        """由 MainWindow._refresh_state 调,刷新统计卡片。"""
        self.card_channels.set_value(str(joined_count), "频道/群组")
        self.card_subscribed.set_value(str(subscribed_count), "正在监听")

        # 登录状态
        state = self._app.client.state
        if state == "ready":
            me = self._app.client.me
            detail = me.get("first_name", "") or me.get("username", "") if me else ""
            self.card_state.set_value("🟢 已登录", detail)
        elif state == "error":
            self.card_state.set_value("🔴 错误", "连接异常")
        elif state in ("phone_required", "uninit"):
            self.card_state.set_value("🟡 未登录", "点击设置→账户登录")
        else:
            self.card_state.set_value(f"⚪ {state}", "")

        # 存储后端
        settings = self._app.settings
        db_label = settings.db_backend.value if settings.db_backend else "?"
        os_label = settings.objectstore_backend.value if settings.objectstore_backend else "?"
        self.card_storage.set_value(f"DB:{db_label}", f"对象:{os_label}")
