# mypy: disable-error-code=attr-defined
# controller 访问 `MainWindow._refresh_state` / `_export_dialog` /
# `reload_shortcuts` 等私有成员 — 跟 `MainWindow` thin delegate 路径
# 共享同一契约,避免 controller 重写一遍包装方法。
"""Header 动作 controller — `_on_header_action` 入口。

2026-10-01 v1.12.x 从 `MainWindow` 抽出。头栏「登录」按钮 → 弹
`LoginDialog`,复用现有代码。

设计:controller 通过 `ctx.main_window` 访问 MainWindow 私有成员
(`_refresh_state`),不需要 callback 注入 — 跟 `tray_controller` /
`theme_controller` 同模式。
"""

from __future__ import annotations

from tgmonitor.ui.controllers._base import MainWindowCtx


class HeaderActionController:
    """头栏「登录 / 验证码 / 2FA 密码」按钮 — 弹 LoginDialog。"""

    def __init__(self, ctx: MainWindowCtx) -> None:
        self._ctx = ctx

    def on_header_action(self) -> None:
        """头栏「登录」按钮 — 弹 LoginDialog(复用现有代码)。"""
        from tgmonitor.ui.widgets.login_dialog import LoginDialog

        dlg = LoginDialog(self._ctx.app, self._ctx.loop, self._ctx.main_window)
        dlg.exec()
        # 登录成功后刷新状态
        self._ctx.main_window._refresh_state()
