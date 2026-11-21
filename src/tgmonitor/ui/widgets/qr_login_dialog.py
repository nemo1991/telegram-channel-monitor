# mypy: disable-error-code="attr-defined"
"""QrLoginDialog — 二维码登录对话框(Telegram App 扫码授权)。

本版本唯一的登录方式:不依赖手机号,也就不受"验证码发到哪个渠道、收不到码"
困扰 —— 已登录的 Telegram App 扫这个二维码即可完成授权。

流程:
  1. 设置页点「二维码登录」→ core `start_qr_login()` 生成 `tg://login?token=…`
     → 本弹窗把 URL 渲染成二维码
  2. 弹窗展示期间持续 `wait_qr_login()`(Telethon 内部监听 UpdateLoginToken);
     用户扫码 → 状态转 ready → signal 回主线程关闭弹窗
  3. 二维码过期 / 失败 → 提示用户「刷新二维码」;关闭弹窗 → cancel 等待任务

跨线程约定:`run_coro` 的回调可能跑在后台 loop 线程,只 emit Qt signal,
所有 widget 操作都在主线程槽里做(项目既有约定,见 async_bridge)。
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import TYPE_CHECKING

import segno
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.events import LoginStateChanged
from tgmonitor.ui.async_bridge import run_coro

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService

log = logging.getLogger(__name__)

_QR_SIZE = 280


def make_qr_pixmap(url: str, *, scale: int = 6, border: int = 2) -> QPixmap:
    """把 `tg://login?token=…` 编码成二维码 QPixmap。

    segno 纯 Python 生成 PNG 字节(不需要 PIL),PySide6 6.12 的
    `loadFromData` format 参数是 str,传 "PNG"。
    """
    qr = segno.make(url, error="m")
    buf = io.BytesIO()
    qr.save(
        buf,
        kind="png",
        scale=scale,
        border=border,
        dark="#1e2433",
        light="#ffffff",
    )
    pix = QPixmap()
    pix.loadFromData(buf.getvalue(), "PNG")
    return pix


class QrLoginDialog(QDialog):
    """二维码登录弹窗:展示二维码 + 等待扫码 + 刷新 / 取消。"""

    # run_coro 回调可能在后台 loop 线程 —— 只 emit,widget 操作在主线程槽。
    qr_url_ready = Signal(str)
    qr_succeeded = Signal()
    qr_failed = Signal(str)

    def __init__(
        self,
        app: AppService,
        loop: asyncio.AbstractEventLoop,
        qr_url: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.app = app
        self.loop = loop
        self._qr_url = qr_url
        self._wait_future: asyncio.Future | None = None
        self._set_busy(False)
        self.setWindowTitle(self.tr("Telegram 二维码登录"))
        self.setModal(True)
        self.setMinimumWidth(_QR_SIZE + 48)

        self._build()
        self.qr_url_ready.connect(self._on_new_url)
        self.qr_succeeded.connect(self._on_succeeded)
        self.qr_failed.connect(self._on_failed)
        self._subscribe_state()
        self._render_qr(qr_url)
        self._start_wait()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)

        self.status_label = QLabel(self.tr("请用 Telegram App 扫描下方二维码完成登录"))
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setMinimumSize(_QR_SIZE, _QR_SIZE)
        root.addWidget(self.qr_label, 1)

        hint = QLabel(self.tr("Telegram App → 设置 → 隐私与安全 → 已登录设备 → 扫描二维码"))
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        root.addWidget(hint)

        bb = QDialogButtonBox()
        self.btn_refresh = bb.addButton(self.tr("刷新二维码"), QDialogButtonBox.ActionRole)
        self.btn_refresh.clicked.connect(self._on_refresh)
        self.btn_cancel = bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.btn_cancel.clicked.connect(self.reject)
        root.addWidget(bb)

    # ---- 状态订阅 / 渲染 ----

    def _subscribe_state(self) -> None:
        async def _on(e: LoginStateChanged) -> None:
            if e.state == "ready":
                self.qr_succeeded.emit()

        self.app.bus.subscribe(LoginStateChanged, _on)

    def _render_qr(self, url: str) -> None:
        self._qr_url = url
        pix = make_qr_pixmap(url)
        self.qr_label.setPixmap(
            pix.scaled(
                _QR_SIZE,
                _QR_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _set_busy(self, busy: bool, msg: str = "") -> None:
        self._busy = busy
        if hasattr(self, "btn_refresh"):
            self.btn_refresh.setEnabled(not busy)
        if busy and msg:
            self.status_label.setText(msg)

    # ---- 等待扫码 ----

    def _start_wait(self) -> None:
        self._set_busy(False, self.tr("等待扫码…"))

        async def _wait() -> tuple[str, str | None]:
            try:
                return await self.app.auth.wait_qr_login()
            except asyncio.CancelledError:
                # 用户关闭弹窗 / 主动刷新 → 静默收尾(不让 run_coro 记 error)。
                return "cancelled", None

        self._wait_future = run_coro(
            self.loop,
            _wait(),
            on_success=lambda res: self._on_wait_result(res),
            on_error=lambda exc: self.qr_failed.emit(str(exc)),
            error_label="wait_qr_login",
        )

    def _on_wait_result(self, result: tuple[str, str | None]) -> None:
        state, detail = result
        if state == "ready":
            self.qr_succeeded.emit()
        elif state == "cancelled":
            return
        else:
            self.qr_failed.emit(detail or self.tr("二维码登录失败"))

    # ---- 刷新 / 结果 ----

    def _on_refresh(self) -> None:
        if self._busy:
            return
        if self._wait_future is not None and not self._wait_future.done():
            self._wait_future.cancel()
        self._set_busy(True, self.tr("正在生成新二维码…"))
        run_coro(
            self.loop,
            self.app.auth.start_qr_login(),
            on_success=self._on_new_qr_result,
            on_error=lambda exc: self.qr_failed.emit(str(exc)),
            error_label="start_qr_login_refresh",
        )

    def _on_new_qr_result(self, result: tuple[str, str | None]) -> None:
        state, detail = result
        if state == "error":
            self._set_busy(False)
            self.qr_failed.emit(detail or self.tr("生成二维码失败"))
        else:
            self.qr_url_ready.emit(detail or "")

    def _on_new_url(self, url: str) -> None:
        if not url:
            self._set_busy(False)
            self.qr_failed.emit(self.tr("生成二维码失败:空 URL"))
            return
        self._render_qr(url)
        self._start_wait()

    def _on_succeeded(self) -> None:
        self.status_label.setText(self.tr("登录成功"))
        self.accept()

    def _on_failed(self, msg: str) -> None:
        self._set_busy(False)
        self.status_label.setText(f"{self.tr('二维码登录失败')}: {msg}")

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802 — Qt 签名
        """关闭弹窗 → cancel 等待任务,避免后台悬挂。"""
        if self._wait_future is not None and not self._wait_future.done():
            self._wait_future.cancel()
        super().closeEvent(event)
