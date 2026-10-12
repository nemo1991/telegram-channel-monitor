"""UiAssembler — `MainWindow` 装配抽提。

2026-10-12 plan-batch3 目标 1:`main_window.py` 982 → ~864 LOC。

把 5 段装配逻辑(共 ~264 LOC)从 `MainWindow` 抽到本类:
- `_build_central_widget`(115 LOC)— widget hierarchy + 5 page + status bar + 2 debounce
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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.ui.nav_bar import VerticalNavBar
from tgmonitor.ui.widgets.channel_widget import ChannelWidget
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget
from tgmonitor.ui.widgets.header_bar import HeaderBar
from tgmonitor.ui.widgets.media_manager_widget import MediaManagerWidget
from tgmonitor.ui.widgets.message_detail import MessageDetail
from tgmonitor.ui.widgets.message_view import MessageView
from tgmonitor.ui.widgets.selection_toolbar import SelectionToolbar
from tgmonitor.ui.widgets.settings_page import SettingsPage

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
        """装配 central widget + 5 个 stacked page + status bar + 1 个 debounce timer。

        原 `MainWindow._build_central_widget`(115 LOC) body 整体搬过来。

        Returns: dict[str, object] — key → widget 引用,key 名跟原 MainWindow
        属性名一致(nav / header / stack / selection_toolbar / live_view /
        message_detail / dashboard / channel_panel / media_manager /
        settings_page / status_bar / media_refresh_debounce / central)。
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

        # 0: 实时流(MessageView + MessageDetail 横向并排)
        # 2026-09-08 v1.7.0:LIVE 顶部增加 _SelectionToolbar(默认 hidden,
        # 多选 >0 才 show)。从单层 QHBoxLayout → QVBoxLayout 嵌套:
        # 上 toolbar / 下 body(QSplitter[live_view, message_detail])。
        # 2026-09-14 v1.7.5 PR #4 (P0-H):QHBoxLayout → QSplitter,用户可拖
        # 详情面板宽度自由(原 280-420 锁死)。splitter 比例 2:1(live 占大头)。
        live_page = QWidget()
        live_layout = QVBoxLayout(live_page)
        live_layout.setContentsMargins(0, 0, 0, 0)
        live_layout.setSpacing(0)
        selection_toolbar = SelectionToolbar()
        selection_toolbar.setVisible(False)
        live_layout.addWidget(selection_toolbar)
        body = QSplitter(Qt.Horizontal)
        body.setContentsMargins(0, 0, 0, 0)
        body.setHandleWidth(1)
        body.setChildrenCollapsible(False)  # 防止用户拖到极窄被隐
        live_view = MessageView()
        message_detail = MessageDetail()
        body.addWidget(live_view)
        body.addWidget(message_detail)
        body.setStretchFactor(0, 2)  # live 优先占 2/3
        body.setStretchFactor(1, 1)  # detail 占 1/3
        body.setSizes([800, 400])  # 启动默认(800px wide → 详情 400)
        live_layout.addWidget(body, 1)
        stack.addWidget(live_page)

        # 1: 大盘
        dashboard = DashboardWidget(self._mw.app, self._mw.monitor, loop=self._mw.loop)
        stack.addWidget(dashboard)

        # 2: 频道
        channels_page = QWidget()
        ch_layout = QVBoxLayout(channels_page)
        ch_layout.setContentsMargins(16, 16, 16, 16)
        ch_layout.setSpacing(12)
        ch_title = QLabel(self._mw.tr("频道管理"))
        ch_title.setObjectName("pageTitle")
        ch_layout.addWidget(ch_title)
        channel_panel = ChannelWidget(self._mw.app, self._mw.loop)
        ch_layout.addWidget(channel_panel, 1)
        stack.addWidget(channels_page)

        # 3: 媒体管理(Media Manager) — 浏览 / 重试 / 删 / 打开 + orphan reconcile
        media_page = QWidget()
        media_layout = QVBoxLayout(media_page)
        media_layout.setContentsMargins(0, 0, 0, 0)
        media_layout.setSpacing(0)
        media_manager = MediaManagerWidget()
        media_layout.addWidget(media_manager)
        stack.addWidget(media_page)

        # 4: 设置
        settings_page = SettingsPage(self._mw.app, self._mw.loop, self._mw.env_path)
        stack.addWidget(settings_page)

        stack.setCurrentIndex(0)
        right_layout.addWidget(stack, 1)

        # 自定义状态栏(2026-10-01 v1.11.x 组件化重构) — 替代 Qt QStatusBar,
        # 9 个子组件(LEFT 5 + RIGHT 4)由 `StatusBar` widget 自管;MainWindow
        # 只做 1 行装配 + 后续 signal 绑 setter。
        from tgmonitor.ui.widgets.status_bar import StatusBar

        status_bar = StatusBar(
            self._mw.app,
            self._mw._vm,
            objects_error=self._mw._objects_error,
            parent=self._mw,
        )

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
            "selection_toolbar": selection_toolbar,
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
