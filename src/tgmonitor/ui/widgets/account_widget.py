"""AccountWidget — 主窗口侧栏上半部。

承担三件事:
1. **凭据编辑**:API ID / API Hash / Phone 表单 + 保存到 .env(并热重载设置)
2. **登录状态展示**:圆点 + 状态文本 + 错误详情
3. **登录动作入口**:在不同 `LoginStateChanged` 下显示不同的输入框与按钮:
   - `phone_required`   →  「登录」
   - `code_required`    →  验证码输入 + 「提交验证码」
   - `password_required`→  2FA 密码输入 + 「提交密码」
   - `ready` / `error`  →  无动作按钮(只读状态卡)

设计原则:这几个操作是用户**每天**会碰的,所以放在常驻侧栏,
而不是藏在 Settings 里。
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.events import AuthErrorOccurred, LoginStateChanged
from tgmonitor.core.settings_store import EditableSettings, update_env_with_settings

if TYPE_CHECKING:
    from pathlib import Path

    from tgmonitor.core.app_service import AppService

log = logging.getLogger(__name__)


# LoginStateChanged.state → QSS 属性。颜色规则见 style.qss
_STATE_TO_DOT = {
    "phone_required": "pending",
    "code_required": "pending",
    "password_required": "pending",
    "ready": "ready",
    "error": "error",
}


class AccountWidget(QGroupBox):
    """侧栏账户 + 登录面板。

    通过 `app` 拿调用,通过 `loop` 派发 async 任务,通过 `env_path` 写 .env。
    """

    def __init__(
        self,
        app: AppService,
        loop: asyncio.AbstractEventLoop,
        env_path: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("账户", parent)
        self.app = app
        self.loop = loop
        self.env_path = env_path
        self._current_state: str = ""
        self._state_detail: str = ""
        self._build()
        self._load_from_settings()
        self._refresh_from_state(self.app.client.state)
        self._wire_bus()

    # ---- UI 装配 ----

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 16, 10, 10)
        root.setSpacing(8)

        # --- 凭据表单 ---
        form = QVBoxLayout()
        form.setSpacing(6)

        # API ID
        row_id = QHBoxLayout()
        row_id.addWidget(QLabel("API ID:"))
        self.in_api_id = QSpinBox()
        self.in_api_id.setRange(0, 2_000_000_000)
        self.in_api_id.setValue(0)
        row_id.addWidget(self.in_api_id, 1)
        form.addLayout(row_id)

        # API Hash
        row_hash = QHBoxLayout()
        row_hash.addWidget(QLabel("API Hash:"))
        self.in_api_hash = QLineEdit()
        self.in_api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.in_api_hash.setPlaceholderText("32 位 hash · my.telegram.org")
        row_hash.addWidget(self.in_api_hash, 1)
        form.addLayout(row_hash)

        # Phone
        row_phone = QHBoxLayout()
        row_phone.addWidget(QLabel("手机号:"))
        self.in_phone = QLineEdit()
        self.in_phone.setPlaceholderText("+8613800000000")
        row_phone.addWidget(self.in_phone, 1)
        form.addLayout(row_phone)

        root.addLayout(form)

        # 保存 + 状态提示
        btn_row = QHBoxLayout()
        self.btn_save = QPushButton("保存到 .env")
        self.btn_save.clicked.connect(self._on_save)
        btn_row.addWidget(self.btn_save, 1)
        self.lbl_cred_note = QLabel("凭据改动需重启 TDLib session 才会生效")
        self.lbl_cred_note.setProperty("role", "hint")
        self.lbl_cred_note.setVisible(False)
        btn_row.addWidget(self.lbl_cred_note, 2)
        root.addLayout(btn_row)

        # --- 状态卡(圆点 + label) ---
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 6, 0, 0)
        self.dot = QFrame()
        self.dot.setObjectName("statusDot")
        self.dot.setProperty("state", "unset")
        self.dot.setFixedSize(16, 16)
        status_row.addWidget(self.dot)
        self.lbl_status = QLabel("—")
        self.lbl_status.setObjectName("statusLabel")
        status_row.addWidget(self.lbl_status, 1)
        root.addLayout(status_row)

        # --- Transient 错误行(验证码错 / 密码错时短暂显示,3 秒后自动消失)---
        self.lbl_transient_err = QLabel("")
        self.lbl_transient_err.setProperty("role", "error")
        self.lbl_transient_err.setVisible(False)
        self.lbl_transient_err.setWordWrap(True)
        root.addWidget(self.lbl_transient_err)
        self._transient_err_timer = None  # type: ignore[assignment]  # QTimer set in _show_transient_error

        # --- 动作区(动态切换) ---
        self.action_box = QFrame()
        self.action_box.setFrameShape(QFrame.Shape.NoFrame)
        al = QVBoxLayout(self.action_box)
        al.setContentsMargins(0, 4, 0, 0)
        al.setSpacing(6)

        # phone_required → 「登录」按钮
        self.btn_login = QPushButton("登录")
        self.btn_login.clicked.connect(self._on_login)
        al.addWidget(self.btn_login)

        # code_required → 验证码输入 + 「提交验证码」
        self.in_code = QLineEdit()
        self.in_code.setPlaceholderText("Telegram 验证码")
        self.btn_submit_code = QPushButton("提交验证码")
        self.btn_submit_code.clicked.connect(self._on_submit_code)
        code_row = QHBoxLayout()
        code_row.addWidget(self.in_code, 1)
        code_row.addWidget(self.btn_submit_code)
        al.addLayout(code_row)

        # password_required → 密码 + 「提交密码」
        self.in_password = QLineEdit()
        self.in_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.in_password.setPlaceholderText("二步验证 2FA 密码")
        self.btn_submit_password = QPushButton("提交密码")
        self.btn_submit_password.clicked.connect(self._on_submit_password)
        pwd_row = QHBoxLayout()
        pwd_row.addWidget(self.in_password, 1)
        pwd_row.addWidget(self.btn_submit_password)
        al.addLayout(pwd_row)

        # ready → 「登出」(纯状态卡,操作就这一个)
        self.btn_logout = QPushButton("登出")
        self.btn_logout.clicked.connect(self._on_logout)
        al.addWidget(self.btn_logout)

        root.addWidget(self.action_box)

        # 初始:全部动作可见性由 _refresh_from_state 控制
        self._show_action("none")
        self.btn_save.setDefault(False)
        # 监听凭据改动,显示 hint
        for w in (self.in_api_id, self.in_api_hash, self.in_phone):
            w.installEventFilter(self)
        self.in_api_id.valueChanged.connect(self._on_cred_changed)
        self.in_api_hash.textChanged.connect(self._on_cred_changed)
        self.in_phone.textChanged.connect(self._on_cred_changed)

    # ---- EventBus → UI ----

    def _wire_bus(self) -> None:
        # 通过 ViewModel-style 同步 bus 事件到主线程,再用 Qt 信号跨线程
        async def _on_state(e) -> None:
            if not isinstance(e, LoginStateChanged):
                return
            self._refresh_from_state(e.state, e.detail)
            # 任何状态切换都把 transient 错误清掉
            self._hide_transient_error()

        self.app.bus.subscribe(LoginStateChanged, _on_state)

        async def _on_auth_err(e) -> None:
            if not isinstance(e, AuthErrorOccurred):
                return
            self._show_transient_error(e.message)

        self.app.bus.subscribe(AuthErrorOccurred, _on_auth_err)

    def _show_transient_error(self, message: str) -> None:
        """在状态卡下方短暂显示一行错误(验证码错 / 密码错),3 秒后自动消失。"""
        from PySide6.QtCore import QTimer
        # 截断过长的消息,UI 空间有限
        display = message[:160]
        self.lbl_transient_err.setText(display)
        self.lbl_transient_err.setVisible(True)
        # 重启定时器 — 同一行连续出错不要叠加闪烁
        if self._transient_err_timer is not None:
            try:
                self._transient_err_timer.stop()
            except RuntimeError:
                pass
        self._transient_err_timer = QTimer(self)
        self._transient_err_timer.setSingleShot(True)
        self._transient_err_timer.timeout.connect(self._hide_transient_error)
        self._transient_err_timer.start(3000)

    def _hide_transient_error(self) -> None:
        self.lbl_transient_err.setVisible(False)
        self.lbl_transient_err.setText("")
        if self._transient_err_timer is not None:
            try:
                self._transient_err_timer.stop()
            except RuntimeError:
                pass
            self._transient_err_timer = None

    # ---- 状态展示 ----

    def _refresh_from_state(self, state: str, detail: str = "") -> None:
        self._current_state = state
        self._state_detail = detail
        dot = _STATE_TO_DOT.get(state, "unset")
        self.dot.setProperty("state", dot)
        # QSS dynamic property — Qt requires re-polish after setProperty
        self.dot.style().unpolish(self.dot)
        self.dot.style().polish(self.dot)

        label, action = _STATE_TO_LABEL.get(state, ("未知", "none"))
        if state == "error" and detail:
            label = f"{label}:{detail[:80]}"
        self.lbl_status.setText(label)
        self._show_action(action)

    def _show_action(self, which: str) -> None:
        # 只显示与当前状态匹配的动作
        self.btn_login.setVisible(which == "phone_required")
        self.in_code.setVisible(which == "code_required")
        self.btn_submit_code.setVisible(which == "code_required")
        self.in_password.setVisible(which == "password_required")
        self.btn_submit_password.setVisible(which == "password_required")
        self.btn_logout.setVisible(which == "ready")
        # 没有动作时(action=none)action_box 也就不必显示
        self.action_box.setVisible(which != "none")

    def _on_logout(self) -> None:
        fut = asyncio.run_coroutine_threadsafe(self.app.client.logout(), self.loop)

        def _on_done(f) -> None:
            try:
                f.result()
            except Exception as exc:  # noqa: BLE001
                log.exception("logout failed: %s", exc)

        fut.add_done_callback(_on_done)

    # ---- 数据 ↔ 控件 ----

    def _load_from_settings(self) -> None:
        e = EditableSettings.from_settings(self.app.settings)
        self.in_api_id.setValue(int(e.api_id))
        self.in_api_hash.setText(e.api_hash)
        self.in_phone.setText(e.phone)
        self._on_cred_changed()  # 重置 hint

    def _collect(self) -> EditableSettings:
        return EditableSettings.from_settings(self.app.settings).__class__(
            api_id=self.in_api_id.value(),
            api_hash=self.in_api_hash.text().strip(),
            phone=self.in_phone.text().strip(),
            session_dir=self.app.settings.session_dir and str(self.app.settings.session_dir) or "./data/session",
            db_backend=self.app.settings.db_backend.value,
            db_dsn=self.app.settings.db_dsn,
            db_root=str(self.app.settings.db_root),
            objectstore_backend=self.app.settings.objectstore_backend.value,
            objectstore_root=str(self.app.settings.objectstore_root),
            objectstore_endpoint=self.app.settings.objectstore_endpoint or "",
            objectstore_region=self.app.settings.objectstore_region,
            objectstore_access_key=self.app.settings.objectstore_access_key or "",
            objectstore_secret_key=self.app.settings.objectstore_secret_key or "",
            objectstore_bucket=self.app.settings.objectstore_bucket,
            media_policy=self.app.settings.media_policy.value,
            data_root=str(self.app.settings.data_root),
            proxy=self.app.settings.proxy or "",
        )

    # ---- 槽 ----

    def _on_cred_changed(self, *_args: object) -> None:
        e = self._collect()
        same = (
            e.api_id == self.app.settings.api_id
            and e.api_hash == self.app.settings.api_hash
            and e.phone == self.app.settings.phone
        )
        self.lbl_cred_note.setVisible(not same)
        self.btn_save.setDefault(not same)

    def _on_save(self) -> None:
        e = self._collect()
        errs = e.validate()
        # proxy 校验报错时允许「无凭据」情形下不阻断保存(让用户先存 API ID / Hash 进入下一步)
        # 但凭据确实不合法就拦住:
        cred_errs = [
            m for m in errs
            if m.startswith("TG_API_") or m.startswith("TG_PHONE")
        ]
        if cred_errs:
            QMessageBox.warning(self, "校验失败", "\n".join(cred_errs))
            return
        if any(m.startswith("TG_PROXY") for m in errs):
            QMessageBox.warning(self, "代理 URL 不合法", "\n".join(errs))
            return
        # 写 .env
        try:
            new_settings = e.to_settings()
            update_env_with_settings(self.env_path, new_settings)
        except OSError as exc:
            QMessageBox.critical(self, ".env 写入失败", str(exc))
            return
        # 立即让 AppService 热重载(凭据变化会发布 needs_relogin)
        fut = asyncio.run_coroutine_threadsafe(
            self.app.reconfigure(new_settings), self.loop
        )

        def _on_done(f) -> None:
            try:
                f.result()
            except Exception as exc:  # noqa: BLE001
                log.exception("reconfigure failed: %s", exc)
            self._on_cred_changed()  # 同步 hint

        fut.add_done_callback(_on_done)

    def _on_login(self) -> None:
        phone = self.in_phone.text().strip() or self.app.settings.phone
        # AppService 内部已做凭据校验;UI 这里不强校验。
        fut = asyncio.run_coroutine_threadsafe(
            self.app.submit_phone(phone), self.loop,
        )
        # UI 状态更新完全靠 LoginStateChanged 推 — done_callback 仅用于
        # 兜底记日志(future 异常)

        def _on_done(f) -> None:
            try:
                f.result()
            except Exception as exc:  # noqa: BLE001
                log.exception("submit_phone failed: %s", exc)

        fut.add_done_callback(_on_done)

    def _on_submit_code(self) -> None:
        code = self.in_code.text().strip()
        if not code:
            return
        fut = asyncio.run_coroutine_threadsafe(self.app.submit_code(code), self.loop)
        self.in_code.clear()

        def _on_done(f) -> None:
            try:
                f.result()
            except Exception as exc:  # noqa: BLE001
                log.exception("submit_code failed: %s", exc)

        fut.add_done_callback(_on_done)

    def _on_submit_password(self) -> None:
        pwd = self.in_password.text()
        if not pwd:
            return
        fut = asyncio.run_coroutine_threadsafe(self.app.submit_password(pwd), self.loop)
        self.in_password.clear()

        def _on_done(f) -> None:
            try:
                f.result()
            except Exception as exc:  # noqa: BLE001
                log.exception("submit_password failed: %s", exc)

        fut.add_done_callback(_on_done)

    # ---- 给 MainWindow / 测试 用的快捷读取 ----

    def state(self) -> tuple[str, str]:
        return self._current_state, self._state_detail


# ---------- 状态映射 ----------

_STATE_TO_LABEL: dict[str, tuple[str, str]] = {
    "phone_required":    ("未登录 — 需要登录",     "phone_required"),
    "code_required":     ("请输入验证码(Telegram 已发)", "code_required"),
    "password_required": ("请输入二步验证 2FA 密码", "password_required"),
    "ready":             ("已登录",               "ready"),
    "error":             ("登录错误",             "none"),
    "closed":            ("会话已关闭 — 请重新登录", "phone_required"),
    "logging_out":       ("正在登出…",           "none"),
    "closing":           ("正在关闭…",           "none"),
    "unknown":           ("未登录 — 需要登录",     "phone_required"),
    "uninit":            ("启动中…",             "none"),
    "":                  ("未登录 — 需要登录",     "phone_required"),
}
