# mypy: disable-error-code=attr-defined
"""应用状态 controller — VM login/conn/error + EventBus notification/login。

2026-10-09 v1.12.x 从 `MainWindow` 抽提。C+D 类 82 LOC 整合:
- VM `login_state` signal → status_bar 提示 + 活动指示器
- VM `conn_state` signal → status_bar 连接状态 + 活动指示器
- VM `error` signal → status_bar 警告 + log
- EventBus `LoginStateChanged` → header.update_state + refresh_state + activity
- EventBus `NotificationRequested` → status_bar 兜底(无 tray 时)

注:`_on_bell_clicked` / `_clear_error_log`(status_bar 错误铃铛 dialog)
也归本 controller 整合(2026-10-09 plan),后续 plan 1.5 UiDialogController
再拆出。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tgmonitor.core.events import LoginStateChanged, NotificationRequested

if TYPE_CHECKING:
    from tgmonitor.core.events import LoginStateChanged, NotificationRequested

log = logging.getLogger(__name__)


class AppStateController:
    """应用状态 — login/conn/error VM signal + EventBus notification/login 整合。"""

    def __init__(self, ctx) -> None:  # ctx: MainWindowCtx(避免循环 import)
        self._ctx = ctx

    def on_login_state(self, state: str) -> None:
        """VM `login_state` signal — 状态栏提示 + 活动指示器。"""
        # 2026-10-01 v1.11.x:showMessage 协议改走 status_bar 自管
        self._ctx.status_bar.show_message(
            self._ctx.main_window.tr("登录状态: {state}").format(state=state), 4000
        )
        # 活动指示器:登录是持续过程,登录中显示中间态,登录完成短暂提示后清空。
        if state in ("ready", "logged_in"):
            self._ctx.status_bar.show_activity("登录完成", timeout_ms=2000)
        elif state in ("error", "closed"):
            self._ctx.status_bar.show_activity(f"登录失败: {state}", timeout_ms=5000)
        else:
            # phone_required / code_required / password_required — 等待用户输入
            self._ctx.status_bar.show_activity(f"登录中: {state}")

    def on_conn_state(self, state: str) -> None:
        """VM `conn_state` signal — 状态栏连接态 + 活动指示器。"""
        # 2026-10-01 v1.11.x:conn 翻译表搬到 status_bar.py,本 controller
        # 只委托 setter(测试桩用 `on_conn_state` 也仍可绑)。
        self._ctx.status_bar.set_connection_state(state)
        # 活动指示器:连接态短促提示,稳态时清空(右侧 conn_label 已是持久指示)
        if state == "ready":
            self._ctx.status_bar.show_activity("TG 已连接", timeout_ms=1500)
        elif state in ("connecting", "updating"):
            self._ctx.status_bar.show_activity(f"TG 连接中: {state}")
        elif state == "waiting_for_network":
            self._ctx.status_bar.show_activity("等待网络…")

    def on_error(self, msg: str) -> None:
        """VM `error` signal — 状态栏警告 + log。"""
        log.warning("error: %s", msg)
        # 2026-10-01 v1.11.x:showMessage 协议改走 status_bar 自管
        self._ctx.status_bar.show_message(f"⚠ {msg}", 5000)
        # 活动指示器同步显示(右侧 conn + 中间 showMessage + 左侧 activity 三处)
        self._ctx.status_bar.show_activity(f"⚠ {msg}", timeout_ms=5000)

    async def on_bus_login(self, e) -> None:
        """EventBus `LoginStateChanged` — header 更新 + refresh_state + activity。

        TDLib 走原始 dict 经 EventBus → UI,与 VM 走 Qt signal 双轨。
        """
        if not isinstance(e, LoginStateChanged):
            return
        self._ctx.header.update_state(e.state, e.detail)
        self._ctx.main_window._refresh_state()
        # 活动指示器:与 `on_login_state` VM slot 同语义
        if e.state in ("ready", "logged_in"):
            self._ctx.status_bar.show_activity("登录完成", timeout_ms=2000)
        elif e.state in ("error", "closed"):
            self._ctx.status_bar.show_activity(f"登录失败: {e.state}", timeout_ms=5000)
        else:
            self._ctx.status_bar.show_activity(f"登录中: {e.state}")

    async def on_notification_fallback(self, event) -> None:
        """无 tray 系统(Linux 无 indicator / offscreen)→ 状态栏 fallback。

        `isinstance` 严格过滤:EventBus publish 任意事件都会触发此 handler
        (因 subscribe 接的是 type object,会按 MRO 匹配),此 handler 只关心
        NotificationRequested。
        """
        if not isinstance(event, NotificationRequested):
            return
        # 系统通知已由 TrayIcon 发出;此处只走状态栏(覆盖两种环境)
        if self._ctx.tray is None or not self._ctx.tray.is_active:
            self._ctx.status_bar.show_message(f"{event.title}: {event.body}", 5000)
        # 活动指示器:无 tray 兜底时也走左侧;有 tray 时左侧仍给提示
        prefix = "⚠ " if event.level == "error" else ("⚡ " if event.level == "warning" else "")
        self._ctx.status_bar.show_activity(f"{prefix}{event.title}: {event.body}", timeout_ms=5000)

    def on_bell_clicked(self) -> None:
        """铃铛点击 — 弹错误日志 dialog。

        2026-10-01 v1.11.x:ring buffer 改由 `StatusBar._ErrorBellButton`
        自管,MainWindow 从 status_bar 取列表构造 dialog。
        2026-10-01 v1.12.x:`ErrorLogDialog` 改用 callback 注入,不再依赖
        `parent` 类型。
        """
        from tgmonitor.ui.widgets.error_log_dialog import ErrorLogDialog

        dlg = ErrorLogDialog(
            self._ctx.status_bar.get_error_log(),
            parent=self._ctx.main_window,
            on_clear=self.on_clear_error_log,
        )
        dlg.exec()

    def on_clear_error_log(self) -> None:
        """清空错误日志(从 dialog 「清空」按钮 / 测试 fixture 调用)。"""
        self._ctx.status_bar.clear_error_log()
