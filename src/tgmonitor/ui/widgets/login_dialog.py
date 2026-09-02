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
        self.setWindowTitle(self.tr("Telegram 登录"))
        self.setModal(True)
        self._expected_state: str = ""
        # 提交期间锁定输入/按钮,防止用户在等待响应时重复提交
        self._busy: bool = False
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
        self.in_code.setPlaceholderText(self.tr("Telegram 发到手机的 5 位验证码"))
        self.in_code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(self.in_code)
        self.stack.addWidget(p_code)
        self.in_code.returnPressed.connect(self._submit_code)

        # page 2: 2FA 密码
        p_pwd = QWidget()
        pwl = QVBoxLayout(p_pwd)
        self.in_pwd = QLineEdit()
        self.in_pwd.setEchoMode(QLineEdit.Password)
        self.in_pwd.setPlaceholderText(self.tr("二步验证 2FA 密码"))
        self.in_pwd.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pwl.addWidget(self.in_pwd)
        self.stack.addWidget(p_pwd)
        self.in_pwd.returnPressed.connect(self._submit_password)

        # page 3: 邮箱地址(2026-08-27 v1.4.0 PR #13:`email_required`)
        p_email = QWidget()
        el = QVBoxLayout(p_email)
        self.in_email = QLineEdit()
        self.in_email.setPlaceholderText("you@example.com")
        self.in_email.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(self.in_email)
        self.stack.addWidget(p_email)
        self.in_email.returnPressed.connect(self._submit_email)

        # page 4: 邮箱验证码(`email_code_required`)
        p_email_code = QWidget()
        ecl = QVBoxLayout(p_email_code)
        self.in_email_code = QLineEdit()
        self.in_email_code.setPlaceholderText(self.tr("邮箱 6 位验证码"))
        self.in_email_code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ecl.addWidget(self.in_email_code)
        self.stack.addWidget(p_email_code)
        self.in_email_code.returnPressed.connect(self._submit_email_code)

        # page 5: 注册(`registration_required`)
        p_reg = QWidget()
        rl = QVBoxLayout(p_reg)
        self.in_reg_first = QLineEdit()
        self.in_reg_first.setPlaceholderText("first name")
        self.in_reg_first.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rl.addWidget(self.in_reg_first)
        self.in_reg_last = QLineEdit()
        self.in_reg_last.setPlaceholderText("last name(可选)")
        self.in_reg_last.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rl.addWidget(self.in_reg_last)
        self.stack.addWidget(p_reg)
        self.in_reg_first.returnPressed.connect(self._submit_registration)
        self.in_reg_last.returnPressed.connect(self._submit_registration)

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
        elif state == "email_required":
            # 2026-08-27 v1.4.0 PR #13:邮箱地址页
            self.status_label.setText("输入邮箱地址")
            self.stack.setCurrentIndex(3)
            self.in_email.setFocus()
        elif state == "email_code_required":
            self.status_label.setText("邮箱验证码")
            self.stack.setCurrentIndex(4)
            self.in_email_code.setFocus()
        elif state == "registration_required":
            self.status_label.setText("注册新账号(需 first_name)")
            self.stack.setCurrentIndex(5)
            self.in_reg_first.setFocus()
        elif state in ("phone_required", "closed", "uninit", "tdlib_parameters"):
            self.status_label.setText("输入手机号(含 + 国家区号)")
            self.stack.setCurrentIndex(0)
            self.in_phone.setFocus()
        elif state == "error":
            # 提交失败 — 展示原因并保持当前页,让用户能重试
            # (此前直接 hide(),用户只看到窗口消失,无失败反馈)
            self.status_label.setText(f"登录失败:{detail or '未知错误'}")
        else:
            self.hide()

    def _set_busy(self, busy: bool, msg: str = "") -> None:
        """提交期间锁定 stack + 提交按钮,防止重复提交;解锁时不改 status_label。"""
        self._busy = busy
        self.stack.setEnabled(not busy)
        self.btn_submit.setEnabled(not busy)
        if busy and msg:
            self.status_label.setText(msg)

    def _show_submit_error(self, prefix: str, exc: BaseException) -> None:
        """run_coro 异常路径 — 把失败原因显示在对话框内(不弹窗、不隐藏)。"""
        self.status_label.setText(f"{prefix}:{exc}")

    # ---- 提交 ----

    def _on_submit(self) -> None:
        if self._expected_state == "code_required":
            self._submit_code()
        elif self._expected_state == "password_required":
            self._submit_password()
        elif self._expected_state == "email_required":
            self._submit_email()
        elif self._expected_state == "email_code_required":
            self._submit_email_code()
        elif self._expected_state == "registration_required":
            self._submit_registration()
        else:
            self._submit_phone()

    def _submit_phone(self) -> None:
        if self._busy:
            return
        phone = self.in_phone.text().strip()
        if not phone:
            return
        self._set_busy(True, "正在请求验证码…")
        fut = run_coro(
            self.loop,
            self.app.submit_phone(phone),
            on_success=lambda res: self._render(res[0], res[1] or ""),
            on_error=lambda exc: self._show_submit_error("提交手机号失败", exc),
            error_label="submit_phone",
        )
        # 无论成功/失败/异常,响应返回后解锁输入
        fut.add_done_callback(lambda _f: self._set_busy(False))

    def _submit_code(self) -> None:
        if self._busy:
            return
        code = self.in_code.text().strip()
        if not code:
            return
        self.in_code.clear()
        self._set_busy(True, "正在验证…")
        # submit_code 返 (state, detail) tuple,_render 接 (state, detail)—
        # run_coro 的 on_success 是 `Callable[[T], None]`,把 tuple 展开传两个位置参。
        fut = run_coro(
            self.loop,
            self.app.submit_code(code),
            on_success=lambda res: self._render(res[0], res[1] or ""),
            on_error=lambda exc: self._show_submit_error("提交验证码失败", exc),
            error_label="submit_code",
        )
        fut.add_done_callback(lambda _f: self._set_busy(False))

    def _submit_password(self) -> None:
        if self._busy:
            return
        pwd = self.in_pwd.text()
        if not pwd:
            return
        self.in_pwd.clear()
        self._set_busy(True, "正在验证…")
        fut = run_coro(
            self.loop,
            self.app.submit_password(pwd),
            on_success=lambda res: self._render(res[0], res[1] or ""),
            on_error=lambda exc: self._show_submit_error("提交 2FA 密码失败", exc),
            error_label="submit_password",
        )
        fut.add_done_callback(lambda _f: self._set_busy(False))

    def _submit_email(self) -> None:
        """2026-08-27 v1.4.0 PR #13:提交邮箱地址。"""
        if self._busy:
            return
        email = self.in_email.text().strip()
        if not email:
            return
        self._set_busy(True, "正在提交邮箱…")
        fut = run_coro(
            self.loop,
            self.app.submit_email(email),
            on_success=lambda res: self._render(res[0], res[1] or ""),
            on_error=lambda exc: self._show_submit_error("提交邮箱失败", exc),
            error_label="submit_email",
        )
        fut.add_done_callback(lambda _f: self._set_busy(False))

    def _submit_email_code(self) -> None:
        """2026-08-27 v1.4.0 PR #13:提交邮箱验证码。"""
        if self._busy:
            return
        code = self.in_email_code.text().strip()
        if not code:
            return
        self.in_email_code.clear()
        self._set_busy(True, "正在验证邮箱…")
        fut = run_coro(
            self.loop,
            self.app.submit_email_code(code),
            on_success=lambda res: self._render(res[0], res[1] or ""),
            on_error=lambda exc: self._show_submit_error("提交邮箱验证码失败", exc),
            error_label="submit_email_code",
        )
        fut.add_done_callback(lambda _f: self._set_busy(False))

    def _submit_registration(self) -> None:
        """2026-08-27 v1.4.0 PR #13:注册新账号。"""
        if self._busy:
            return
        first = self.in_reg_first.text().strip()
        if not first:
            self.status_label.setText("first_name 必填")
            return
        last = self.in_reg_last.text().strip()
        self._set_busy(True, "正在注册…")
        fut = run_coro(
            self.loop,
            self.app.submit_registration(first, last),
            on_success=lambda res: self._render(res[0], res[1] or ""),
            on_error=lambda exc: self._show_submit_error("注册失败", exc),
            error_label="submit_registration",
        )
        fut.add_done_callback(lambda _f: self._set_busy(False))


def app_get_state(app) -> str:  # 顶层 helper,避免循环 import
    """同步读取当前 client state — 仅用于 UI 初次显示,非 hot-path。"""
    try:
        return app.client.state
    except Exception:  # noqa: BLE001
        return "unknown"
