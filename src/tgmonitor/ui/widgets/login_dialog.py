# mypy: disable-error-code="attr-defined"
"""LoginDialog — 鉴权交互的兜底对话框(手机号 / 验证码 / 2FA)。

凭据主要在设置页填写(账户凭证组);此对话框是用户在主界面直接点「登录」
时的交互入口,按当前登录状态就地切换:
- `phone_required` / `closed` / `uninit` → 手机号页,提交后触发验证码下发
- `code_required` → 验证码页
- `password_required` → 2FA 密码页

弹窗期间 Telegram 状态变化(LoginStateChanged)会自动切页,用户无需重开。
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
from tgmonitor.ui._async import run_coro

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService

log = logging.getLogger(__name__)


class LoginDialog(QDialog):
    """鉴权交互对话框(手机号 → 验证码 → 2FA 按状态就地切换)。"""

    def __init__(
        self,
        app: AppService,
        loop: asyncio.AbstractEventLoop,
        parent: QWidget | None = None,
    ) -> None:
        """建 modal dialog + 自动按当前 LoginState 选页(手机号 / 验证码 / 2FA)。"""
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

        # page 0: 手机号
        p_phone = QWidget()
        pl = QVBoxLayout(p_phone)
        self.in_phone = QLineEdit()
        self.in_phone.setPlaceholderText("+8613800000000")
        self.in_phone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 预填设置里的手机号(设置页「账户凭证」里填的那个)
        try:
            cur = self.app.settings.phone
            if isinstance(cur, str) and cur.strip():
                self.in_phone.setText(cur.strip())
        except Exception:  # noqa: BLE001
            log.debug("prefill phone failed", exc_info=True)
        pl.addWidget(self.in_phone)
        self.stack.addWidget(p_phone)
        self.in_phone.returnPressed.connect(self._submit_phone)

        # page 1: 验证码
        p_code = QWidget()
        cl = QVBoxLayout(p_code)
        self.in_code = QLineEdit()
        self.in_code.setPlaceholderText("Telegram 发到手机的 5 位验证码")
        self.in_code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(self.in_code)
        self.stack.addWidget(p_code)
        self.in_code.returnPressed.connect(self._submit_code)

        # page 2: 2FA 密码
        p_pwd = QWidget()
        pwl = QVBoxLayout(p_pwd)
        self.in_pwd = QLineEdit()
        self.in_pwd.setEchoMode(QLineEdit.Password)
        self.in_pwd.setPlaceholderText("二步验证 2FA 密码")
        self.in_pwd.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pwl.addWidget(self.in_pwd)
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
            state = app_get_state(self.app)
            self._render(state, "")
        except Exception:  # noqa: BLE001
            log.exception("init LoginDialog state")

    def _render(self, state: str, detail: str = "") -> None:
        self._expected_state = state
        if state == "code_required":
            self.status_label.setText("Telegram 验证码")
            self.stack.setCurrentIndex(1)
            self.in_code.setFocus()
        elif state == "password_required":
            self.status_label.setText("二步验证 2FA 密码")
            self.stack.setCurrentIndex(2)
            self.in_pwd.setFocus()
        elif state in ("phone_required", "closed", "uninit", "tdlib_parameters"):
            self.status_label.setText("输入手机号(含 + 国家区号)")
            self.stack.setCurrentIndex(0)
            self.in_phone.setFocus()
        else:
            self.hide()

    # ---- 提交 ----

    def _on_submit(self) -> None:
        if self._expected_state == "code_required":
            self._submit_code()
        elif self._expected_state == "password_required":
            self._submit_password()
        else:
            self._submit_phone()

    def _submit_phone(self) -> None:
        phone = self.in_phone.text().strip()
        if not phone:
            return
        run_coro(
            self.loop, self.app.submit_phone(phone),
            on_success=lambda res: self._render(res[0], res[1] or ""),
            error_label="submit_phone",
        )

    def _submit_code(self) -> None:
        code = self.in_code.text().strip()
        if not code:
            return
        self.in_code.clear()
        # submit_code 返 (state, detail) tuple,_render 接 (state, detail)—
        # run_coro 的 on_success 是 `Callable[[T], None]`,把 tuple 展开传两个位置参。
        run_coro(
            self.loop, self.app.submit_code(code),
            on_success=lambda res: self._render(res[0], res[1] or ""),
            error_label="submit_code",
        )

    def _submit_password(self) -> None:
        pwd = self.in_pwd.text()
        if not pwd:
            return
        self.in_pwd.clear()
        run_coro(
            self.loop, self.app.submit_password(pwd),
            on_success=lambda res: self._render(res[0], res[1] or ""),
            error_label="submit_password",
        )


def app_get_state(app) -> str:  # 顶层 helper,避免循环 import
    """同步读取当前 client state — 仅用于 UI 初次显示,非 hot-path。"""
    try:
        return app.client.state
    except Exception:  # noqa: BLE001
        return "unknown"
