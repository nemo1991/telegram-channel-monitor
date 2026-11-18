"""UiAssembler — `MainWindow` 装配抽提。

2026-10-12 plan-batch3 目标 1:`main_window.py` 982 → ~864 LOC。

把 5 段装配逻辑(共 ~264 LOC)从 `MainWindow` 抽到本类:
- `_build_central_widget`(115 LOC)— widget hierarchy + 3 page + status bar + 2 debounce
- `_build_menu`(31 LOC)— File menu
- `_build_tray`(15 LOC)— TrayIcon
- `_wire_signals`(42 LOC)— 内部 signal 连接
- `_wire_events`(46 LOC)— VM event 连接

设计原则:
- **plain class**(不是 QObject)—— `UiAssembler` 不持 Qt 主线程所有权,
  只是 build → return widget dict,再走 signal wire。
- **持 `MainWindow` 引用** —— build + wire 都通过 `self._mw` 访问
  `MainWindow` 属性(如 `_objects_error` / `tr(...)` / `setCentralWidget` / `menuBar()`)。
  装配 QAction / QMenu / QSplitter 子项时 parent 必是 `MainWindow`,避免循环。
- **`MainWindowCtx` 持有 widget refs** —— 返回的 widget dict 由 MainWindow
  显式赋值给 ctx 字段,ctx 不持有 UiAssembler 引用(避免循环)。

为什么这样切:
- `MainWindow.__init__` 装配顺序敏感(ctx 先就位 → controllers 实例化 →
  signal wire 顺序),plain class UiAssembler 不打乱这个顺序。
- `_wire_*` 方法的 `self.X.connect(...)` 需要 access MainWindow 的 widget 引用,
  UiAssembler 持 `main_window` 引用后等价于原 `self.X.connect(...)`。

不做:
- 不抽 controller(已有 15 controller + MainWindowctx,plan-batch2 已落点)
- 不抽 `_build_actions` / `_build_menus` / `_build_toolbars`(中央 widget 装配在
  `_build_central_widget` 内部,不分更细)
- 不改 `_setup_core` / `_setup_controllers`(这两个是 stage + ctx 装配逻辑,
  plan-batch2 阶段 3.2 已拆)
"""

# mypy: disable-error-code="attr-defined"
# PySide6 6.6+.pyi 漏了一堆 class-level const(QFrame.NoFrame /
# Qt.Horizontal)—同 main_window.py 顶部一致(2026-10-12 v1.12.x)。

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.events import QuitRequested
from tgmonitor.ui.nav_bar import VerticalNavBar
from tgmonitor.ui.widgets.channel_widget import ChannelWidget
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget
from tgmonitor.ui.widgets.header_bar import HeaderBar
from tgmonitor.ui.widgets.media_manager_widget import MediaManagerWidget
from tgmonitor.ui.widgets.message_detail import MessageDetail
from tgmonitor.ui.widgets.message_view import MessageView
from tgmonitor.ui.widgets.settings_page import SettingsPage
from tgmonitor.ui.widgets.tray_icon import TrayIcon

if TYPE_CHECKING:
    from tgmonitor.ui.main_window import MainWindow


class UiAssembler:
    """Pure widget hierarchy + signal orchestration tool.

    plan-batch3 目标 1 step 1.2:`build_central()` 从 `MainWindow._build_central_widget`
    整体搬过来,返回 widget dict 由 MainWindow 显式赋值给 self 属性。

    设计:`UiAssembler` 只持 `MainWindow` 引用,**不需要 ctx**。原因:
    - build 阶段只读 `MainWindow` 的 state/VM/QWidget 基础(`self.app` /
      `self.monitor` / `self.loop` / `self.env_path` / `self._objects_error` /
      `self._vm` / `self.tr(...)`)
    - 返回的 widget dict 由 `MainWindow` 显式 `self.X = widgets["X"]`
    - `MainWindowCtx` 在 `_setup_controllers` 里创建,读 `self.X` 属性
    - 这样装配顺序保持不变(`build_central` 在 `_setup_controllers` 之前调)
    """

    def __init__(self, main_window: MainWindow) -> None:
        """保存 main_window 引用,不持有任何 Qt 资源。"""
        self._mw = main_window

    def build_central(self) -> dict[str, object]:
        """装配 central widget + 3 个 stacked page + status bar + 1 个 debounce timer。

        原 `MainWindow._build_central_widget`(115 LOC) body 整体搬过来。

        2026-11-18 v2.0.6:撤销独立频道页,ChannelWidget 单实例迁入设置页。
        页序:0 大盘(含实时流) / 1 媒体 / 2 设置(含监听频道管理)。

        Returns: dict[str, object] — key → widget 引用,key 名跟原 MainWindow
        属性名一致(nav / header / stack / live_view / message_detail /
        dashboard / channel_panel / media_manager / settings_page / status_bar /
        media_refresh_debounce / central)。
        """
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 左: 导航栏 ----
        nav = VerticalNavBar()
        root.addWidget(nav)

        # ---- 右: 头栏 + 内容 + 状态栏 ----
        right = QWidget()
        right.setObjectName("contentArea")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # 紧凑头栏
        header = HeaderBar()
        right_layout.addWidget(header)

        # QStackedWidget 内容页
        stack = QStackedWidget()
        stack.setFrameShape(QFrame.NoFrame)

        # 2026-11-17 v2.0.4 FR-03:实时流单实例迁入大盘,撤销独立 LIVE 页。
        # MessageView / MessageDetail 仍由 MainWindow 持引用(控制器与测试依赖),
        # 但 reparent 进 DashboardWidget 内部的 QSplitter。导航 5→4,大盘为首页。
        live_view = MessageView()
        message_detail = MessageDetail()

        # 0: 大盘(首页;含实时流 + 最近活动 | 频道统计)
        # channels_provider 让频道统计表能读 VM 的真实 title/kind。
        dashboard = DashboardWidget(
            self._mw.app,
            self._mw.monitor,
            loop=self._mw.loop,
            channels_provider=lambda: self._mw._vm.known_channels,
            live_view=live_view,
            message_detail=message_detail,
        )
        stack.addWidget(dashboard)

        # 2026-11-18 v2.0.6:撤销独立频道页 —— ChannelWidget 单实例迁入
        # SettingsPage(监听频道管理分组)。MainWindow/controllers 仍持
        # channel_panel 引用不变,只是 parent 链改到设置页内。
        # 先建实例,构造 SettingsPage 时注入;assembler 仍把它放进返回 dict。
        channel_panel = ChannelWidget(self._mw.app, self._mw.loop)

        # 1: 媒体管理(Media Manager) — 浏览 / 重试 / 删 / 打开 + orphan reconcile
        media_page = QWidget()
        media_layout = QVBoxLayout(media_page)
        media_layout.setContentsMargins(0, 0, 0, 0)
        media_layout.setSpacing(0)
        media_manager = MediaManagerWidget()
        media_layout.addWidget(media_manager)
        stack.addWidget(media_page)

        # 2: 设置
        settings_page = SettingsPage(
            self._mw.app,
            self._mw.loop,
            self._mw.env_path,
            channel_panel=channel_panel,
        )
        stack.addWidget(settings_page)

        stack.setCurrentIndex(0)
        right_layout.addWidget(stack, 1)

        # 自定义状态栏(2026-10-01 v1.11.x 组件化重构) — 替代 Qt QStatusBar,
        # 9 个子组件(LEFT 5 + RIGHT 4)由 `StatusBar` widget 自管;MainWindow
        # 只做 1 行装配 + 后续 signal 绑 setter。
        # 2026-11-16 v2.0.1:parent 必须给 right 且要 addWidget 进
        # right_layout。只给 parent 不会加入布局 —— 状态栏会成为 MainWindow
        # 的直接子 widget,geometry 停在 (0,0) 浮在窗口顶部(用户报的 bug 1)。
        from tgmonitor.ui.widgets.status_bar import StatusBar

        status_bar = StatusBar(
            self._mw.app,
            self._mw._vm,
            objects_error=self._mw._objects_error,
            parent=right,
        )
        right_layout.addWidget(status_bar)

        # Media Manager 批量删除 debounce(2026-09-28 fix):100 条连续 MediaDeleted
        # 只刷一次 widget。timer 放在 MainWindow 上,与 StatusBar 解耦。
        # 2026-10-04 plan 阶段 3.3:`timeout` 连接移到 `_setup_controllers`,
        # 跟 `MediaActionsController.flush_media_refresh_pending` 接。

        media_refresh_debounce = QTimer(self._mw)
        media_refresh_debounce.setSingleShot(True)
        media_refresh_debounce.setInterval(100)

        # 启动提示 — 替代旧 `status_bar.showMessage(self.tr("就绪"))`
        status_bar.show_message(self._mw.tr("就绪"), 3000)

        root.addWidget(right, 1)

        return {
            "central": central,
            "nav": nav,
            "header": header,
            "stack": stack,
            "live_view": live_view,
            "message_detail": message_detail,
            "dashboard": dashboard,
            "channel_panel": channel_panel,
            "media_manager": media_manager,
            "settings_page": settings_page,
            "status_bar": status_bar,
            "media_refresh_debounce": media_refresh_debounce,
        }

    def assemble(self) -> dict[str, object]:
        """编排入口 —— plan-batch3 目标 1 step 1.5 落地。"""
        msg = "assemble() is not yet wired — see plan-batch3-1.2~1.5"
        raise NotImplementedError(msg)

    def build_menu(self) -> dict[str, object]:
        """File menu — 「显示主窗口 / 暂停监听 / 退出」(2026-08-30 v1.5.0 PR #A4)。

        原 `MainWindow._build_menu`(31 LOC)body 整体搬过来。`act_show` /
        `act_quit` 由 MainWindow 显式赋值给 self 属性。`act_pause` 是
        lambda-bound,不需要持久属性。

        Returns:
            dict[str, object] — key → QAction 引用:act_show / act_pause /
            act_quit + menu_bar 引用。
        """
        menu_bar = self._mw.menuBar()
        file_menu = menu_bar.addMenu("&File")
        act_show = QAction(self._mw.tr("显示主窗口"), self._mw)
        act_show.triggered.connect(self._mw._show_and_raise)
        file_menu.addAction(act_show)
        file_menu.addSeparator()
        act_pause = QAction(self._mw.tr("暂停监听"), self._mw)
        act_pause.triggered.connect(
            lambda: self._mw.app.bus.publish_threadsafe(self._mw.loop, QuitRequested(pause=True))
        )
        file_menu.addAction(act_pause)
        act_quit = QAction(self._mw.tr("退出"), self._mw)
        act_quit.triggered.connect(self._mw._quit_app)
        file_menu.addAction(act_quit)

        return {
            "menu_bar": menu_bar,
            "act_show": act_show,
            "act_pause": act_pause,
            "act_quit": act_quit,
        }

    def build_tray(self) -> TrayIcon:
        """系统托盘图标(2026-08-30 v1.5.0 PR #A4)。

        原 `MainWindow._build_tray`(15 LOC)body 整体搬过来。`isSystemTrayAvailable()`
        假时(offscreen / Linux 无 indicator)— tray 仍创建(订阅 handler 内部
        no-op),但 `is_active=False`,closeEvent 不进入 minimize-to-tray 分支,
        直接走 shutdown。

        Returns:
            TrayIcon — MainWindow 显式赋值给 `self._tray`。
        """
        tray = TrayIcon(self._mw, self._mw.app)
        if tray.is_active:
            tray.show()
        # 2026-10-09 v1.12.x:NotificationRequested 订阅挪到 `_wire_signals`
        # 阶段(原 `_build_tray` 早于 `_setup_controllers`,controller 还未
        # 实例化)。TrayIcon 内部仍订一份做系统通知;此处补订做状态栏兜底。
        return tray

    def wire_signals(self) -> None:
        """集中所有信号连接(原 `MainWindow._wire_signals`,2026-10-04 plan 阶段 3.1)。

        4 大类(2026-11-17 v2.0.3:D3 头栏纯标题 + D4 大盘只读化):
        - nav → MainWindow 内部槽
        - ChannelWidget → 业务回调(刷新频道 / 全量同步)
        - MediaManagerWidget → vm 或本地回调
        - MessageView / MessageDetail → 点击显示详情
        登录/登出入口已迁到 SettingsPage 自管;大盘不再注入操作回调。

        依赖 controller 的连接**留在** `_setup_controllers` 完成。
        """
        mw = self._mw
        # ---- 信号连接 ----
        mw.nav.current_changed.connect(mw.stack.setCurrentIndex)
        # Media Manager 页切换时自动拉一次列表(VM 异步,首次可能空 → 等频道就绪)
        mw.nav.current_changed.connect(mw._on_nav_changed)
        # 2026-11-17 v2.0.3 D3/D4:header 按钮与 dashboard 三按钮接线删除。
        # 登录/登出入口由 SettingsPage 自管;刷新/全量同步仍在 ChannelWidget,
        # 消息导出 UI 入口按用户确认移除(core 能力保留)。

        # ChannelWidget 信号
        mw.channel_panel.refresh_button.clicked.connect(mw._on_refresh_channels)
        mw.channel_panel.sync_requested.connect(mw._on_sync_requested)

        # MessageView → MessageDetail(点击消息显示详情)
        mw.live_view.message_selected.connect(mw.message_detail.show_message)

        # 2026-11-16 v2.0.2:LIVE 页只读化 — 多选浮层 / 批量操作入口整体移除。

    def wire_events(self) -> None:
        """ViewModel 事件绑定(原 `MainWindow._wire_events`)。

        5 大类:
        - VM download progress → media_manager
        - VM channels / export / settings / sync / 搜索 → MainWindow 槽
        - VM media_list / reconcile / channel_cleared → media_manager
        - VM media_downloaded / media_retry / media_deleted → MediaActionsController
          (signal wire 在 `_setup_controllers`,避免提前引用 `_media_actions_ctrl`)

        注释里记录的「提前引用 controller 触发 AttributeError」约束:搜索 / 多选
        / message_stream 等依赖 controller 的连接**仍在** `_setup_controllers`
        完成(本方法只接管不依赖 controller 的连接)。
        """
        mw = self._mw

        # ---- VM signal → media_manager ----
        # 2026-09-01 v1.5.1 PR #B3:下载进度反馈 — VM signal → media_manager
        # 直接刷新对应 row 的「已下载 X / Y (Z%)」文字。
        mw._vm.media_download_progress.connect(mw.media_manager.on_download_progress)
        # Media Manager 转发(2026-08-24)
        mw._vm.media_list_loaded.connect(mw.media_manager.on_media_loaded)
        mw._vm.media_reconcile_done.connect(mw.media_manager.on_reconcile_done)
        # 按频道批量删除反馈(2026-08-25 PR #4)
        mw._vm.channel_cleared.connect(mw.media_manager.on_channel_cleared)
        # 缩略图 VM signal — 直接绑 widget(2026-08-25 PR #1)
        mw.media_manager.set_view_model(mw._vm)

        # ---- VM signal → MainWindow 内部槽 ----
        # 2026-10-09 v1.12.x:VM login/conn/error wire 挪到 wire_signals 阶段
        # (原 wire_events 早于 _setup_controllers,controller 还未实例化)。
        mw._vm.channels_changed.connect(mw._refresh_state)
        mw._vm.export_done.connect(mw._on_export_done)
        mw._vm.settings_changed.connect(mw._on_settings_changed)
        # 2026-09-25 v1.8.x:同步/导出进度 → 状态栏左侧活动指示器。
        mw._vm.sync_progress.connect(mw._on_sync_progress)
        mw._vm.sync_done.connect(mw._on_sync_done)
        mw._vm.export_progress.connect(mw._on_export_progress)
        # 2026-11-16 v2.0.2:VM 搜索结果信号随顶部搜索功能移除。
