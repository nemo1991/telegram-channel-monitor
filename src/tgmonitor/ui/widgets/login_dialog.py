"""LoginDialog — 仅当账户凭据已填、但 TDLib 要求验证码或 2FA 时弹出。

凭据表单已经搬到主窗口 AccountWidget;这个对话框**只剩 escape hatch**:
- 收到 `code_required` 事件 → 自动弹,输入验证码提交
- 收到 `password_required` 事件 → 自动弹,输入 2FA 密码提交
- 也可工具栏菜单显式调出(未来扩展)。

多数情况下 AccountWidget 就地切换输入框就够了;此对话框是当用户已离开
账户面板时(比如最小化、或者未在主窗口时弹)Telegram 突然继续问问题的兜底。
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.events import LoginStateChanged

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService

log = logging.getLogger(__name__)


class LoginDialog(QDialog):
    def __init__(
        self,
        app: AppService,
        loop: asyncio.AbstractEventLoop,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.app = app
        self.loop = loop
        self.setWindowTitle("Telegram 登录")
        self.setModal(True)
        self._expected_state: str = ""
        self._build()
        # 自动订阅,按当前状态展示对应页
        self._auto_show()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        self.status_label = QLabel("…")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status_label)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # page 0: 验证码
        p_code = QWidget(); cl = QVBoxLayout(p_code)
        self.in_code = QLineEdit()
        self.in_code.setPlaceholderText("Telegram 发到手机的 5 位验证码")
        self.in_code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(self.in_code)
        self.stack.addWidget(p_code)
        self.in_code.returnPressed.connect(self._submit_code)

        # page 1: 2FA 密码
        p_pwd = QWidget(); pl = QVBoxLayout(p_pwd)
        self.in_pwd = QLineEdit()
        self.in_pwd.setEchoMode(QLineEdit.Password)
        self.in_pwd.setPlaceholderText("二步验证 2FA 密码")
        self.in_pwd.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pl.addWidget(self.in_pwd)
        self.stack.addWidget(p_pwd)
        self.in_pwd.returnPressed.connect(self._submit_password)

        # 按钮
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        self.btn_submit = bb.addButton("提交", QDialogButtonBox.AcceptRole)
        self.btn_submit.clicked.connect(self._on_submit)
        root.addWidget(bb)

    # ---- 自动呈现 ----

    def _auto_show(self) -> None:
        async def _on(e: LoginStateChanged) -> None:
            self._render(e.state, e.detail)

        self.app.bus.subscribe(LoginStateChanged, _on)
        # 初次拉当前状态
        try:
            state = app_get_state(self.app)  # type: ignore[name-defined]
            self._render(state, "")
        except Exception:  # noqa: BLE001
            log.exception("init LoginDialog state")

    def _render(self, state: str, detail: str = "") -> None:
        self._expected_state = state
        if state == "code_required":
            self.status_label.setText("Telegram 验证码")
            self.stack.setCurrentIndex(0)
            self.in_code.setFocus()
        elif state == "password_required":
            self.status_label.setText("二步验证 2FA 密码")
            self.stack.setCurrentIndex(1)
            self.in_pwd.setFocus()
        else:
            self.hide()

    # ---- 提交 ----

    def _on_submit(self) -> None:
        if self._expected_state == "code_required":
            self._submit_code()
        elif self._expected_state == "password_required":
            self._submit_password()

    def _submit_code(self) -> None:
        code = self.in_code.text().strip()
        if not code:
            return
        fut = asyncio.run_coroutine_threadsafe(self.app.submit_code(code), self.loop)
        self.in_code.clear()

        def _on_done(f) -> None:
            try:
                state = f.result()
            except Exception as exc:  # noqa: BLE001
                log.exception("submit_code failed: %s", exc)
                return
            self._render(state)

        fut.add_done_callback(_on_done)

    def _submit_password(self) -> None:
        pwd = self.in_pwd.text()
        if not pwd:
            return
        fut = asyncio.run_coroutine_threadsafe(self.app.submit_password(pwd), self.loop)
        self.in_pwd.clear()

        def _on_done(f) -> None:
            try:
                state = f.result()
            except Exception as exc:  # noqa: BLE001
                log.exception("submit_password failed: %s", exc)
                return
            self._render(state)

        fut.add_done_callback(_on_done)


def app_get_state(app) -> str:  # 顶层 helper,避免循环 import
    """同步读取当前 client state — 仅用于 UI 初次显示,非 hot-path。"""
    try:
        return app.client.state
    except Exception:  # noqa: BLE001
        return "unknown"
