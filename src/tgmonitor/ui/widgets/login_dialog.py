"""LoginDialog — 登录状态机对应的 UI。

按当前 state 切换显示 phone → code → 2FA password → ready。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService


class LoginDialog(QDialog):
    def __init__(self, app: AppService, loop: asyncio.AbstractEventLoop, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.loop = loop
        self.setWindowTitle("Telegram 登录")
        self.setModal(True)
        self._build()
        self._init_state()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        self.status = QLabel("…")
        root.addWidget(self.status)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        # 页面 0: phone
        p0 = QWidget(); f0 = QFormLayout(p0)
        self.in_phone = QLineEdit(self.app.settings.phone)
        self.in_phone.setPlaceholderText("+8613800000000")
        f0.addRow("手机号:", self.in_phone)
        self.btn_phone = bb.addButton("发送验证码", QDialogButtonBox.AcceptRole)
        self.btn_phone.clicked.connect(self._submit_phone)
        self.stack.addWidget(p0)

        # 页面 1: code
        p1 = QWidget(); f1 = QFormLayout(p1)
        self.in_code = QLineEdit()
        f1.addRow("验证码:", self.in_code)
        self.btn_code = bb.addButton("提交验证码", QDialogButtonBox.AcceptRole)
        self.btn_code.clicked.connect(self._submit_code)
        self.stack.addWidget(p1)

        # 页面 2: 2FA password
        p2 = QWidget(); f2 = QFormLayout(p2)
        self.in_pwd = QLineEdit(); self.in_pwd.setEchoMode(QLineEdit.Password)
        f2.addRow("2FA 密码:", self.in_pwd)
        self.btn_pwd = bb.addButton("提交密码", QDialogButtonBox.AcceptRole)
        self.btn_pwd.clicked.connect(self._submit_password)
        self.stack.addWidget(p2)

        # 页面 3: ready
        p3 = QLabel("已登录 ✓")
        p3.setAlignment(Qt.AlignCenter)
        self.stack.addWidget(p3)

    def _init_state(self) -> None:
        state = self.app.client.state
        self._show(state)

    def _show(self, state: str) -> None:
        self.status.setText(f"当前状态: {state}")
        idx = {"phone_required": 0, "code_required": 1, "password_required": 2, "ready": 3}.get(state, 0)
        self.stack.setCurrentIndex(idx)
        if state == "ready":
            QMessageBox.information(self, "登录", "登录成功")

    # ---- 提交 ----

    def _submit_phone(self) -> None:
        phone = self.in_phone.text().strip()
        if not phone:
            return
        fut = asyncio.run_coroutine_threadsafe(self.app.login(phone), self.loop)
        state = fut.result()
        self._show(state)

    def _submit_code(self) -> None:
        code = self.in_code.text().strip()
        if not code:
            return
        fut = asyncio.run_coroutine_threadsafe(self.app.submit_code(code), self.loop)
        state = fut.result()
        self._show(state)

    def _submit_password(self) -> None:
        pwd = self.in_pwd.text()
        if not pwd:
            return
        fut = asyncio.run_coroutine_threadsafe(self.app.submit_password(pwd), self.loop)
        state = fut.result()
        self._show(state)
