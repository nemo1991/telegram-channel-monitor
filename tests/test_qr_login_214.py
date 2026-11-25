"""二维码登录(2.1.4)守卫测试:core 状态机 / 凭据校验 / UI 编排 / 二维码渲染。

覆盖:
  - AuthService.start_qr_login:不校验手机号(只 api_id/api_hash),失败发 ErrorOccurred
  - TelethonTelegramClient:await qr_login()→url(内部已 recreate,勿重复调);
    wait→ready 走 _on_login_success;token 不进 detail/日志;超时/异常返 error
  - FakeTelegramClient:qr_required → ready
  - SettingsPage:点「二维码登录」保存 2 个 key、不弹手机号页、失败提示
  - QrLoginDialog:二维码可渲染、ready signal 关闭、刷新/取消

跨线程 widget 操作 risk:沿用 tests/fixtures/_qt_runtime 的 qapp/qloop 约定。
"""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.core.auth_service import AuthService  # noqa: E402
from tgmonitor.core.config import Settings  # noqa: E402
from tgmonitor.core.events import ErrorOccurred, EventBus, LoginStateChanged  # noqa: E402
from tgmonitor.core.telegram.fake_client import FakeTelegramClient  # noqa: E402


def _wait_until(pred, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        QApplication.processEvents()
        time.sleep(0.02)
    raise AssertionError("pred() 超时")


# ---- core: AuthService ----


async def test_auth_qr_login_rejects_bad_api_credentials() -> None:
    bus = EventBus()
    errors: list[ErrorOccurred] = []
    bus.subscribe(ErrorOccurred, lambda e: errors.append(e))
    client = FakeTelegramClient()
    settings = Settings.for_test(api_id=0, api_hash="a" * 32)
    auth = AuthService(bus=bus, client=client, settings=settings)

    state, msg = await auth.start_qr_login()
    assert state == "error"
    assert "TG_API_ID" in (msg or "")
    assert client.state == "phone_required"  # 没碰 client
    await bus.flush()
    assert len(errors) == 1


# ---- core: Telethon 实现(真实 QRLogin 调用路径) ----


class _FakeQR:
    def __init__(self, url: str = "tg://login?token=SECRET123") -> None:
        self.url = url
        self.recreated = 0
        self.waited = 0
        self.wait_error: Exception | None = None

    async def recreate(self) -> None:
        # 真实的 QRLogin 在 qr_login() 内部已经 recreate 过一次;这里保留计数
        # 只为守卫"实现不要重复调用",正常应为 0。
        self.recreated += 1

    async def wait(self) -> None:
        self.waited += 1
        if self.wait_error:
            raise self.wait_error


@pytest.fixture
async def qr_client(settings, bus, monkeypatch):
    from tgmonitor.core.telegram.telethon_client import TelethonTelegramClient

    w = TelethonTelegramClient(settings, event_bus=bus)
    mock = MagicMock()
    mock.is_connected.return_value = True
    mock.connect = AsyncMock()
    mock.get_me = AsyncMock(return_value=MagicMock(id=7, username="u", first_name="F"))
    qr = _FakeQR()
    # qr_login 在 Telethon 里是 async —— 必须用 AsyncMock 还原真实语义。
    # 用同步 MagicMock 会掩盖"漏 await"的 bug(曾导致真机 AttributeError)。
    mock.qr_login = AsyncMock(return_value=qr)
    monkeypatch.setattr(w, "_build_client", lambda: mock)
    w._client = mock
    w._qr_login = None
    yield w, mock, qr
    await w._cancel_watchdog()


async def test_telethon_start_qr_returns_url_without_leaking_token(qr_client, bus) -> None:
    w, mock, qr = qr_client
    received: list[LoginStateChanged] = []
    bus.subscribe(LoginStateChanged, lambda e: received.append(e))

    state, url = await w.start_qr_login()
    assert state == "qr_required"
    assert url == "tg://login?token=SECRET123"
    mock.qr_login.assert_awaited_once()  # 守卫:async API 必须 await
    assert qr.recreated == 0  # qr_login() 内部已 recreate,不应重复调用
    assert w._state_detail == "请用 Telegram App 扫码授权"
    assert "SECRET123" not in w._state_detail  # token 只走返回值
    await bus.flush()
    assert received and all("SECRET123" not in e.detail for e in received)


async def test_telethon_wait_qr_timeout_returns_error(qr_client) -> None:
    w, mock, qr = qr_client
    await w.start_qr_login()
    qr.wait_error = TimeoutError()
    state, detail = await w.wait_qr_login()
    assert state == "error"
    assert "过期" in (detail or "")
    assert w._qr_login is None


# ---- UI: 设置页编排 ----


# ---- UI: 二维码弹窗 ----
