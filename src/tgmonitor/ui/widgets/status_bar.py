"""自定义 StatusBar + 9 个子组件 — 替代 Qt QStatusBar。

2026-10-01 v1.11.x 状态栏组件化重构:

背景:之前 `MainWindow._build_ui` 把 7 个 widget 全堆在一处 inline 组装,
加上 6 个 slot 直接 mutate widget 实例,职责全压在 MainWindow 一个
2500+ 行的文件里。同时 dashboard 上的 4 类实时信息(存储后端 / 监听
频道数 / 最近消息时间 / 当前选中频道)只在 dashboard 页面可见,切
到其他页面就看不到,违反「主窗口顶部/底部永远可见」的 UI 原则。

新设计:
- 自定义 `StatusBar`(QWidget + QHBoxLayout)整体替代 QStatusBar,不再用
  `addWidget` / `addPermanentWidget` / 原生 `showMessage` 协议。
- 9 个子组件,LEFT(5)+ RIGHT(4)固定布局,各自独立可测。
- 自实现 transient 消息(QTimer + QLabel),保留 showMessage 短促提示语义。
- 子组件状态完全自有;MainWindow 只剩 ~15 行组装 + 11 个 thin slot。

LEFT 区(stretch 比例:SelectedChannel=0 / Stats=0 / Backend=0 /
LastMessage=0 / Activity=1):
    _SelectedChannelLabel  当前选中频道,空时显示灰色提示
    _StatsLabel            「监听:N 消息:M」
    _BackendLabel          「DB:{db} OS:{os}」
    _LastMessageLabel      「最后:HH:MM」
    _ActivityLabel         现有 activity + throttle 逻辑(从 MainWindow 搬来)

RIGHT 区(stretch=0):
    _ObjectsWarnLabel      ⚠ 对象存储不可用(可空)
    _ConnectionLabel       TG 连接状态
    _PausedLabel           ⏸ 暂停监听
    _ErrorBellButton       🔔 N 错误铃铛(内部 EventBus 订阅 + ring buffer)
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from tgmonitor.core.events import AuthErrorOccurred

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel

log = logging.getLogger(__name__)

# ---- 翻译表 ----

# 2026-09-07 v1.6.8:conn state 翻译表 — module-level,跟 state_labels 同样
# 走 QCoreApplication.translate 路径,无需 QObject。这样 test 用 _FakeWindow
# (无 tr()) 也能正确取到 zh_CN / en_US 译文。
_CONN_STATE_LABEL_SRC: dict[str, str] = {
    "waiting_for_network": "TG 等待网络",
    "connecting": "TG 连接中…",
    "updating": "TG 同步中…",
    "ready": "TG 已连接",
    "unknown": "TG 状态未知",
}


def _conn_state_label(state: str) -> str:
    """Return translated label for Telegram connection state."""
    src = _CONN_STATE_LABEL_SRC.get(state)
    if src is not None:
        return QCoreApplication.translate("status_bar", src)
    return QCoreApplication.translate("status_bar", "TG {state}").format(state=state)


# 鉴权错误 source → 中文 kind 翻译表(原 MainWindow._on_bus_auth_error 内
# inline dict,搬到这里让 _ErrorBellButton handler 独立用)。
_AUTH_SOURCE_KIND_SRC: dict[str, str] = {
    "code": "验证码错误",
    "password": "两步验证密码错误",
    "phone": "手机号错误",
    "telegram_internal": "Telegram 内部错误",
}


def _auth_source_kind(source: str) -> str:
    """鉴权错误 source → 中文 kind(翻译表 miss 时兜底「鉴权错误」)。"""
    src = _AUTH_SOURCE_KIND_SRC.get(source)
    if src is not None:
        return QCoreApplication.translate("status_bar", src)
    return QCoreApplication.translate("status_bar", "鉴权错误")


# ======================== LEFT 区子组件 ========================


class _SelectedChannelLabel(QLabel):
    """LEFT:当前选中频道。无选中时显示灰色提示。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.setObjectName("statusSelectedChannelLabel")
        self.setMinimumWidth(80)
        self._render(None)

    def set_channel(self, name: str | None) -> None:
        self._render(name)

    def _render(self, name: str | None) -> None:
        if name:
            self.setText(f"📡 {name}")
            self.setStyleSheet("color: palette(text);")
        else:
            self.setText(QCoreApplication.translate("status_bar", "(无选中频道)"))
            self.setStyleSheet("color: palette(placeholder);")


class _StatsLabel(QLabel):
    """LEFT:「监听:N 消息:M」。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.setObjectName("statusStatsLabel")
        self.setMinimumWidth(120)

    def set_stats(self, subscribed: int, messages: int) -> None:
        text = QCoreApplication.translate("status_bar", "监听:{n_ch} 消息:{n_msg}").format(
            n_ch=subscribed, n_msg=messages
        )
        self.setText(text)


class _BackendLabel(QLabel):
    """LEFT:「DB:{db} OS:{os}」。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.setObjectName("statusBackendLabel")
        self.setMinimumWidth(140)
        # 启动时尚未设置,显示灰色占位
        self._render(None)

    def set_label(self, label: str | None) -> None:
        self._render(label)

    def _render(self, label: str | None) -> None:
        if label:
            self.setText(label)
            self.setStyleSheet("color: palette(text);")
        else:
            self.setText(QCoreApplication.translate("status_bar", "DB:— OS:—"))
            self.setStyleSheet("color: palette(placeholder);")


class _LastMessageLabel(QLabel):
    """LEFT:「最后:HH:MM」(展示最近一条收到消息的时间)。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.setObjectName("statusLastMessageLabel")
        self.setMinimumWidth(100)
        self._render(None)

    def set_time(self, when: datetime | None) -> None:
        self._render(when)

    def _render(self, when: datetime | None) -> None:
        if when is None:
            self.setText(QCoreApplication.translate("status_bar", "最后:—"))
            self.setStyleSheet("color: palette(placeholder);")
            return
        # 只显示 HH:MM,日期默认今天;非今天(很少见)显示日期。
        now = datetime.now(UTC)
        if when.date() == now.date():
            prefix = QCoreApplication.translate("status_bar", "最后")
            self.setText(f"{prefix}:{when.strftime('%H:%M')}")
            self.setStyleSheet("color: palette(text);")
        else:
            prefix = QCoreApplication.translate("status_bar", "最后")
            self.setText(f"{prefix}:{when.strftime('%m-%d %H:%M')}")
            self.setStyleSheet("color: palette(text);")


class _ActivityLabel(QLabel):
    """LEFT(stretch=1):活动 + 节流。

    从 MainWindow._show_activity / _throttle_activity 搬来,行为完全一致:
    - `show_text(text, *, timeout_ms)` 立即更新文本,timeout_ms>0 时到点清空。
    - `throttle(key, text, *, min_interval_ms, timeout_ms)` 节流,同 key
      在 min_interval_ms 内只更新 1 次。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.setObjectName("statusActivityLabel")
        self.setMinimumWidth(200)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._throttle: dict[str, float] = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self.setText(""))

    def show_text(self, text: str, *, timeout_ms: int | None = None) -> None:
        self._timer.stop()
        self.setText(text)
        if timeout_ms is not None and timeout_ms > 0:
            self._timer.start(timeout_ms)

    def throttle(
        self,
        key: str,
        text: str,
        min_interval_ms: int,
        timeout_ms: int | None = None,
    ) -> None:
        now = time.monotonic() * 1000.0
        last = self._throttle.get(key, 0.0)
        if now - last < min_interval_ms:
            return
        self._throttle[key] = now
        self.show_text(text, timeout_ms=timeout_ms)


# ======================== RIGHT 区子组件 ========================


class _ConnectionLabel(QLabel):
    """RIGHT:TG 连接状态(持久指示)。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(_conn_state_label("unknown"), parent)
        self.setObjectName("statusConnectionLabel")
        self.setMinimumWidth(80)

    def set_state(self, state: str) -> None:
        self.setText(_conn_state_label(state))


class _PausedLabel(QLabel):
    """RIGHT:⏸ 暂停监听(默认 hidden,接 monitoring_paused 显示)。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            QCoreApplication.translate("status_bar", "⏸ 暂停监听"),
            parent,
        )
        self.setObjectName("statusPausedLabel")
        self.setStyleSheet(
            "background-color: #f7c948; color: #333; padding: 2px 8px;"
            " border-radius: 3px; font-weight: 600;"
        )
        self.setToolTip(
            QCoreApplication.translate(
                "status_bar",
                "监听已暂停 — 实时更新与媒体下载已停。tray 菜单点「继续监听」恢复",
            )
        )
        self.setVisible(False)

    def set_visible(self, paused: bool) -> None:
        self.setVisible(paused)


class _ErrorBellButton(QPushButton):
    """RIGHT:🔔 N 错误铃铛 + ring buffer + EventBus 订阅。

    2026-09-14 v1.7.5 PR #6 (P0-K) 从 MainWindow._on_bus_auth_error 搬来:
    - 内部 ring buffer(上限 100 条 `(when, source, msg)`)
    - EventBus 直接订阅 `AuthErrorOccurred`(本项目 widget-private 状态
      走 EventBus 直接订阅,跟 dashboard_widget 同模式)。
    - bell_clicked signal 暴露给 MainWindow 弹 dialog。
    - 收 AuthErrorOccurred 时同步弹 QMessageBox.warning(验证码错误时必
      须显式确认才能关,避免用户以为没输过重新再输再超时)。
    """

    bell_clicked = Signal()

    def __init__(self, app: AppService, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.setObjectName("errorBellBtn")
        self.setToolTip(QCoreApplication.translate("status_bar", "查看错误日志"))
        self.setVisible(False)
        self.setFlat(True)
        self.clicked.connect(self.bell_clicked.emit)

        self._error_log: list[tuple[datetime, str, str]] = []
        self._app = app
        # 直接订阅 EventBus — widget 私有状态(ring buffer),不污染 MainWindow
        app.bus.subscribe(AuthErrorOccurred, self._on_bus_auth_error)

    async def _on_bus_auth_error(self, e: object) -> None:
        """收到 AuthErrorOccurred → 压 ring buffer + 显铃铛 + 弹 QMessageBox。

        MainWindow 之前 inline 这个 handler;搬过来后语义不变:
          1. ring buffer 仅留最近 100 条,防内存膨胀
          2. 铃铛按钮显示 + 自增计数(纯图标「🔔」 → 「🔔 N」)
          3. 弹 QMessageBox.warning(icon=Critical,非自动消失)
        """
        from PySide6.QtWidgets import QMessageBox  # noqa: PLC0415

        if not isinstance(e, AuthErrorOccurred):
            return
        when = datetime.now(UTC)
        self._error_log.append((when, e.source, e.message))
        if len(self._error_log) > 100:
            self._error_log = self._error_log[-100:]
        n = len(self._error_log)
        self.setVisible(True)
        self.setText(QCoreApplication.translate("status_bar", "🔔 {n}").format(n=n))

        kind = _auth_source_kind(e.source)
        # 取当前 widget 所在的顶层窗口作 parent(dialog 模态中心正确);widget 单独
        # 弹出时取 widget 顶层窗口做父;若 widget 尚未加入窗口树,fallback widget 自身。
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415

        qt_app = QApplication.instance()
        parent = self.window() if self.window() is not None else self
        if qt_app is not None:
            QMessageBox.warning(
                parent,
                QCoreApplication.translate("status_bar", "⚠ {kind}").format(kind=kind),
                (
                    f"{e.message}\n\n"
                    + QCoreApplication.translate(
                        "status_bar", "详细错误日志可点击状态栏「🔔 {n}」按钮查看。"
                    ).format(n=n)
                ),
                QMessageBox.StandardButton.Ok,
            )

    def get_log(self) -> list[tuple[datetime, str, str]]:
        """供 MainWindow._on_bell_clicked 取 ring buffer 内容弹 dialog。"""
        return list(self._error_log)

    def clear(self) -> None:
        """清空 ring buffer + 隐藏铃铛(MainWindow._clear_error_log 改走这里)。"""
        self._error_log.clear()
        self.setVisible(False)


class _ObjectsWarnLabel(QLabel):
    """RIGHT:⚠ 对象存储不可用(红字常驻,SettingsChanged 后被移除)。"""

    def __init__(self, error: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statusObjectsWarnLabel")
        self.setText(
            QCoreApplication.translate("status_bar", "⚠ 对象存储不可用: {err}").format(err=error)
        )
        self.setStyleSheet("color: #d03030; font-weight: 600;")
        self.setToolTip(
            QCoreApplication.translate(
                "status_bar",
                "媒体文件将无法下载 / 保存。请到 设置 → 对象存储 检查配置"
                "(S3/MinIO 填 API 地址,勿填控制台地址)后重新保存。",
            )
        )


# ======================== 主组件 StatusBar ========================


class StatusBar(QWidget):
    """替代 QStatusBar:全自管 LEFT(5)+ RIGHT(4)共 9 个子组件。

    API:
        set_connection_state(state)         — TG 连接状态
        set_paused(paused)                  — ⏸ 暂停监听
        set_selected_channel(name)          — 当前选中频道
        set_stats(*, subscribed, messages)   — 监听频道数 / 消息累计
        set_backend_label(label)            — DB/OS 后端名
        set_last_message_time(when)         — 最后收到消息时间
        show_activity(text, *, timeout_ms)  — 左侧活动指示
        throttle_activity(key, ...)          — 节流版 show_activity
        show_message(text, timeout_ms)       — 中间 transient 提示
        clear_message()                      — 立即清掉 transient

    EventBus / VM 绑定:
        委托给主窗口在 `_wire_events` 走 `self._vm.<signal>.connect(...)`,
        直接绑到 status_bar 的公开 setter / 子组件 setter;本类不主动订
        EventBus(除 `_ErrorBellButton` widget-private)。
    """

    def __init__(
        self,
        app: AppService,
        vm: MonitorViewModel,
        *,
        objects_error: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("customStatusBar")
        self._app = app
        self._vm = vm

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 2, 12, 2)
        layout.setSpacing(8)

        # LEFT — 5 个子组件,Activity stretch=1 占剩余空间
        self._selected_channel = _SelectedChannelLabel()
        self._stats = _StatsLabel()
        self._backend = _BackendLabel()
        self._last_message = _LastMessageLabel()
        self._activity = _ActivityLabel()
        layout.addWidget(self._selected_channel)
        layout.addWidget(self._stats)
        layout.addWidget(self._backend)
        layout.addWidget(self._last_message)
        layout.addWidget(self._activity, 1)

        # RIGHT — 4 个子组件
        self._objects_warn: _ObjectsWarnLabel | None = None
        if objects_error:
            self._objects_warn = _ObjectsWarnLabel(objects_error)
            layout.addWidget(self._objects_warn)
        self._connection = _ConnectionLabel()
        self._paused = _PausedLabel()
        self._bell = _ErrorBellButton(app)
        layout.addWidget(self._connection)
        layout.addWidget(self._paused)
        layout.addWidget(self._bell)

        # transient 消息层 — 自实现 showMessage 协议(替代 QStatusBar.showMessage)
        self._transient = QLabel("", self)
        self._transient.setObjectName("statusTransientLabel")
        self._transient.setStyleSheet("color: palette(text); font-style: italic;")
        # 注意:transient label 不直接 addWidget — 用户通过 show_message/clear_message
        # 控制文本,内部用一个独立的 QTimer 实现 timeout;widget 自身可见性永远 True。
        self._transient_timer = QTimer(self)
        self._transient_timer.setSingleShot(True)
        self._transient_timer.timeout.connect(lambda: self._transient.setText(""))

    # ---- 子组件 setter(公开方法)----

    def set_connection_state(self, state: str) -> None:
        self._connection.set_state(state)

    def set_paused(self, paused: bool) -> None:
        self._paused.set_visible(paused)

    def set_selected_channel(self, name: str | None) -> None:
        self._selected_channel.set_channel(name)

    def set_stats(self, *, subscribed: int, messages: int) -> None:
        self._stats.set_stats(subscribed, messages)

    def set_backend_label(self, label: str) -> None:
        self._backend.set_label(label)

    def set_last_message_time(self, when: datetime | None) -> None:
        self._last_message.set_time(when)

    # ---- activity(原 _show_activity / _throttle_activity 委托)----

    def show_activity(self, text: str, *, timeout_ms: int | None = None) -> None:
        self._activity.show_text(text, timeout_ms=timeout_ms)

    def throttle_activity(
        self,
        key: str,
        text: str,
        min_interval_ms: int,
        timeout_ms: int | None = None,
    ) -> None:
        self._activity.throttle(key, text, min_interval_ms, timeout_ms)

    # ---- transient 消息(原 status_bar.showMessage 替代)----

    def show_message(self, text: str, timeout_ms: int = 5000) -> None:
        self._transient.setText(text)
        self._transient_timer.start(timeout_ms)

    def clear_message(self) -> None:
        self._transient_timer.stop()
        self._transient.setText("")

    # ---- 子组件访问器(MainWindow 弹铃铛 dialog 用)----

    def get_error_log(self) -> list[tuple[datetime, str, str]]:
        """铃铛 ring buffer 内容(MainWindow._on_bell_clicked 透传给 dialog)。"""
        return self._bell.get_log()

    def clear_error_log(self) -> None:
        """清空铃铛 ring buffer(原 MainWindow._clear_error_log)。"""
        self._bell.clear()

    # ---- SettingsChanged 处理:移除 objects_warn ----

    def on_settings_changed(self) -> None:
        """`vm.settings_changed` 信号触发 — 移除 _objects_warn(若存在)。

        原 MainWindow._on_settings_changed(L1824-1827)处理逻辑搬到这里,
        widget 自管生命周期,不再需要 MainWindow 持有 `_objects_warn_label`
        字段。
        """
        if self._objects_warn is not None:
            self._objects_warn.setParent(None)
            self._objects_warn.deleteLater()
            self._objects_warn = None
