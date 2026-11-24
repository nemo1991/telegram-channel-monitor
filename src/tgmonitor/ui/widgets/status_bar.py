"""自定义 StatusBar + 7 个子组件 — 替代 Qt QStatusBar。

2026-10-01 v1.11.x 状态栏组件化重构:
  之前 `MainWindow._build_ui` 把 7 个 widget 全堆在一处 inline 组装,
  加上 6 个 slot 直接 mutate widget 实例,职责全压在 MainWindow 一个
  2500+ 行的文件里。抽出自定义 `StatusBar`(QWidget + QHBoxLayout),
  子组件独立可测,MainWindow 只留 thin slot。

2026-11-17 v2.0.3 D1/D2 精简重排:
  - 剔除低价值项:`_BackendLabel`(DB:OS 开发态信息)与 `_LastMessageLabel`
    (最后消息时间与大盘消息总数重复)。
  - 登录态从右上 HeaderBar 迁入底部,与网络态**合并为单一状态位**
    `_UnifiedStateLabel`:未 ready 时显示 12 态登录态,ready 时显示
    5 态连接态;不再有两个都叫「状态」、口径还不同的标签。

LEFT 区(Stats=0 / Activity=1):
    _StatsLabel            「监听:N 消息:M」
    _ActivityLabel         现有 activity + throttle 逻辑

RIGHT 区(stretch=0):
    _ObjectsWarnLabel      警告 对象存储不可用(可空)
    _UnifiedStateLabel     唯一状态位(登录态 / 连接态)
    _PausedLabel           ⏸ 暂停监听
    _ErrorBellButton       铃铛 N 错误铃铛(内部 EventBus 订阅 + ring buffer)

自实现 transient 消息(QTimer + QLabel),保留 showMessage 短促提示语义。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from tgmonitor.core.events import AuthErrorOccurred
from tgmonitor.ui.state_labels import state_color_bucket, state_dot, state_hint, state_label

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.ui.viewmodels.monitor_vm import MonitorViewModel

log = logging.getLogger(__name__)

# ---- 翻译表 ----

# 2026-09-07 v1.6.8:conn state 翻译表 — module-level,跟 state_labels 同样
# 走 QCoreApplication.translate 路径,无需 QObject。这样 test 用 _FakeWindow
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


class _UnifiedStateLabel(QLabel):
    """RIGHT:唯一状态位 — 登录态优先,ready 时显示 TG 连接态。

    2026-11-17 v2.0.3 D2:右上 HeaderBar 的 12 态登录徽章迁到底部,与原有
    5 态连接标签合并。优先级规则(不新造口径,复用两张现成翻译表):
      - `login_state != "ready"` → `state_dot/state_label`(12 态),
        包括 error / code_required 等需要用户行动的状态;
      - `login_state == "ready"` → `_conn_state_label`(5 态)。
    两条更新路径(VM `login_state` / EventBus `LoginStateChanged`)都写
    `set_login_state`,连接态仍写 `set_connection_state`;两者互不覆盖。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(_conn_state_label("unknown"), parent)
        self.setObjectName("statusStateLabel")
        self.setMinimumWidth(120)
        self._login_state = "uninit"
        self._conn_state = "unknown"
        self._render()

    def set_login_state(self, state: str) -> None:
        self._login_state = state
        self._render()

    def set_connection_state(self, state: str) -> None:
        self._conn_state = state
        self._render()

    def _render(self) -> None:
        # 2026-11-19 v2.0.7 FR-02:唯一状态位挂语义色档位,QSS 负责色号。
        if self._login_state != "ready":
            self.setText(f"{state_dot(self._login_state)} {state_label(self._login_state)}")
            self.setToolTip(state_hint(self._login_state))
            self._set_state_bucket(self._login_state)
        else:
            self.setText(_conn_state_label(self._conn_state))
            self.setToolTip(state_hint("ready"))
            self._set_state_bucket(self._conn_state)

    def _set_state_bucket(self, state: str) -> None:
        bucket = state_color_bucket(state)
        if self.property("state") == bucket:
            return
        self.setProperty("state", bucket)
        # 动态 property 变化后需重新 polish 才会应用新 QSS 规则。
        self.style().unpolish(self)
        self.style().polish(self)


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
    """RIGHT:铃铛 N 错误铃铛 + ring buffer + EventBus 订阅。

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
          2. 铃铛按钮显示 + 自增计数(纯图标「铃铛」 → 「铃铛 N」)
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
        self.setText(QCoreApplication.translate("status_bar", "错误 {n}").format(n=n))

        kind = _auth_source_kind(e.source)
        # 取当前 widget 所在的顶层窗口作 parent(dialog 模态中心正确);widget 单独
        # 弹出时取 widget 顶层窗口做父;若 widget 尚未加入窗口树,fallback widget 自身。
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415

        qt_app = QApplication.instance()
        parent = self.window() if self.window() is not None else self
        if qt_app is not None:
            QMessageBox.warning(
                parent,
                QCoreApplication.translate("status_bar", "警告 {kind}").format(kind=kind),
                (
                    f"{e.message}\n\n"
                    + QCoreApplication.translate(
                        "status_bar", "详细错误日志可点击状态栏「错误 {n}」按钮查看。"
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
    """RIGHT:警告 对象存储不可用(红字常驻,SettingsChanged 后被移除)。"""

    def __init__(self, error: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statusObjectsWarnLabel")
        self.setText(
            QCoreApplication.translate("status_bar", "对象存储不可用: {err}").format(err=error)
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
    """替代 QStatusBar:全自管 LEFT(3)+ RIGHT(4)共 7 个子组件。

    API:
        set_login_state(state)              — 12 态登录状态(合并状态位)
        set_connection_state(state)         — 5 态 TG 连接状态(合并状态位)
        set_paused(paused)                  — ⏸ 暂停监听
        set_stats(*, subscribed, messages)   — 监听频道数 / 消息累计
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

        # LEFT — Stats + 提示区(Activity/Transient 并排,stretch=1 占剩余空间)
        self._stats = _StatsLabel()
        self._activity = _ActivityLabel()
        layout.addWidget(self._stats)
        hint_layout = QHBoxLayout()
        hint_layout.setContentsMargins(0, 0, 0, 0)
        hint_layout.setSpacing(8)
        hint_layout.addWidget(self._activity, 1)
        layout.addLayout(hint_layout, 1)

        # RIGHT — 4 个子组件(ObjectsWarn 按需)
        self._objects_warn: _ObjectsWarnLabel | None = None
        if objects_error:
            self._objects_warn = _ObjectsWarnLabel(objects_error)
            layout.addWidget(self._objects_warn)
        self._state = _UnifiedStateLabel()
        self._paused = _PausedLabel()
        self._bell = _ErrorBellButton(app)
        layout.addWidget(self._state)
        layout.addWidget(self._paused)
        layout.addWidget(self._bell)

        # transient 消息层 — 挂进左侧提示区(与 activity 并排),不再游离于布局外。
        # 原实现 `QLabel("", self)` 没 addWidget,几何停在默认 (0,0,100,30):
        # 文字中心比布局内标签低约 3px,且和 stats 重叠 —— 登录时
        # `show_message("登录状态: ready")` 就表现为「ready 偏下」。
        self._transient = QLabel("", self)
        self._transient.setObjectName("statusTransientLabel")
        self._transient.setStyleSheet("color: palette(text); font-style: italic;")
        self._transient.setVisible(False)  # 空文本不占位,有消息才显示
        # 插在 activity 之前、不抢 stretch:临时消息紧跟统计文字按内容宽度显示,
        # activity 仍占剩余空间把右侧状态位推到最右。
        hint_layout.insertWidget(0, self._transient)
        self._transient_timer = QTimer(self)
        self._transient_timer.setSingleShot(True)
        self._transient_timer.timeout.connect(self.clear_message)

    # ---- 子组件 setter(公开方法)----

    def set_login_state(self, state: str) -> None:
        """登录态(12 态)写入唯一状态位 — 未 ready 时优先显示。"""
        self._state.set_login_state(state)

    def set_connection_state(self, state: str) -> None:
        """连接态(5 态)写入唯一状态位 — 仅 ready 时呈现。"""
        self._state.set_connection_state(state)

    def set_paused(self, paused: bool) -> None:
        self._paused.set_visible(paused)

    def set_stats(self, *, subscribed: int, messages: int) -> None:
        self._stats.set_stats(subscribed, messages)

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
        self._transient.setVisible(bool(text))
        self._transient_timer.start(timeout_ms)

    def clear_message(self) -> None:
        self._transient_timer.stop()
        self._transient.setText("")
        self._transient.setVisible(False)

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
