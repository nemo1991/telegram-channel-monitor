# mypy: disable-error-code=attr-defined
"""Settings 变更 reaction controller — `_on_settings_changed`。

2026-10-01 v1.12.x 从 `MainWindow` 抽出。设置保存后:
1. status_bar.on_settings_changed 移除 _objects_warn 标签
2. status_bar 提示 + 活动指示器
3. needs_relogin / needs_restart 弹 QMessageBox 提示

设计:controller 通过 `ctx.main_window` 访问 MainWindow 私有成员(`tr`)。
"""

from __future__ import annotations

from tgmonitor.ui.controllers._base import MainWindowCtx


class SettingsReactionController:
    """Settings 热重载 — UI 状态同步 + 弹窗提示。"""

    def __init__(self, ctx: MainWindowCtx) -> None:
        self._ctx = ctx

    def on_settings_changed(
        self,
        what: str,
        needs_relogin: bool,
        needs_restart: bool,
        backend_label: str,
    ) -> None:
        mw = self._ctx.main_window
        # 2026-10-01 v1.11.x 状态栏组件化:_objects_warn 改由 status_bar 自管,
        # MainWindow 委托 `on_settings_changed` 移除。
        self._ctx.status_bar.on_settings_changed()
        msg = mw.tr("已热重载: {what} → {backend}").format(what=what, backend=backend_label)
        # 2026-10-01 v1.11.x:showMessage 协议改走 status_bar 自管
        self._ctx.status_bar.show_message(msg, 5000)
        self._ctx.status_bar.show_activity(msg, timeout_ms=5000)
        if needs_relogin:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(
                self._ctx.main_window,
                mw.tr("凭据已变更"),
                mw.tr("Telegram 凭据已变更。\n请重新登录以继续监听。"),
            )
        elif needs_restart:
            # v1.0.23:proxy / session_dir 是 TelegramClient 构造参数,运行时
            # 不重建 client,变更已写入 .env 但需重启应用才生效
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(
                self._ctx.main_window,
                "需重启生效",
                "代理或会话目录已变更并保存。\nTelegram 客户端在启动时创建,请重启应用使其生效。",
            )
