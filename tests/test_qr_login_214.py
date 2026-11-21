"""二维码登录(2.1.4)守卫测试:core 状态机 / 凭据校验 / UI 编排 / 二维码渲染。

覆盖:
  - AuthService.start_qr_login:不校验手机号(只 api_id/api_hash),失败发 ErrorOccurred
  - TelethonTelegramClient:qr_login→recreate→url;wait→ready 走 _on_login_success;
    token 不进 detail/日志;超时/异常返 error
  - FakeTelegramClient:qr_required → ready
  - SettingsPage:点「二维码登录」保存 2 个 key、不弹手机号页、失败提示
  - QrLoginDialog:二维码可渲染、ready signal 关闭、刷新/取消

跨线程 widget 操作 risk:沿用 tests/fixtures/_qt_runtime 的 qapp/qloop 约定。
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from tgmonitor.core.auth_service import AuthService  # noqa: E402
from tgmonitor.core.config import Settings  # noqa: E402
from tgmonitor.core.events import ErrorOccurred, EventBus, LoginStateChanged  # noqa: E402
from tgmonitor.core.settings_store import parse_env_file  # noqa: E402
from tgmonitor.core.telegram.fake_client import FakeTelegramClient  # noqa: E402
from tgmonitor.core.telegram.unconfigured import UnconfiguredTelegramClient  # noqa: E402
from tgmonitor.ui.widgets.qr_login_dialog import QrLoginDialog, make_qr_pixmap  # noqa: E402
from tgmonitor.ui.widgets.settings_page import SettingsPage  # noqa: E402


def _wait_until(pred, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        QApplication.processEvents()
        time.sleep(0.02)
    raise AssertionError("pred() 超时")


# ---- core: AuthService ----


async def test_auth_qr_login_does_not_require_phone() -> None:
    """二维码路径只校验 api 凭据 —— 首次配置手机号为空也能生成二维码。"""
    bus = EventBus()
    client = FakeTelegramClient()
    settings = Settings.for_test(api_id=123, api_hash="a" * 32, phone="")
    auth = AuthService(bus=bus, client=client, settings=settings)

    state, url = await auth.start_qr_login()
    assert state == "qr_required"
    assert url and url.startswith("tg://login?token=")
    assert client.state == "qr_required"


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


async def test_auth_wait_qr_login_promotes_to_ready() -> None:
    bus = EventBus()
    client = FakeTelegramClient()
    settings = Settings.for_test(api_id=123, api_hash="a" * 32, phone="")
    auth = AuthService(bus=bus, client=client, settings=settings)
    await auth.start_qr_login()

    state, _ = await auth.wait_qr_login()
    assert state == "ready"
    assert client.me is not None


async def test_unconfigured_client_qr_methods_safe() -> None:
    client = UnconfiguredTelegramClient()
    assert (await client.start_qr_login())[0] == "error"
    assert (await client.wait_qr_login())[0] == "error"


# ---- core: Telethon 实现(真实 QRLogin 调用路径) ----


class _FakeQR:
    def __init__(self, url: str = "tg://login?token=SECRET123") -> None:
        self.url = url
        self.recreated = 0
        self.waited = 0
        self.wait_error: Exception | None = None

    async def recreate(self) -> None:
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
    mock.qr_login = MagicMock(return_value=qr)
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
    assert qr.recreated == 1
    assert w._state_detail == "请用 Telegram App 扫码授权"
    assert "SECRET123" not in w._state_detail  # token 只走返回值
    await bus.flush()
    assert received and all("SECRET123" not in e.detail for e in received)


async def test_telethon_wait_qr_success_promotes_ready(qr_client) -> None:
    w, mock, qr = qr_client
    await w.start_qr_login()
    state, _ = await w.wait_qr_login()
    assert state == "ready"
    assert qr.waited == 1
    assert w.me is not None and w.me["id"] == 7
    assert w._qr_login is None  # 会话用完即清


async def test_telethon_wait_qr_timeout_returns_error(qr_client) -> None:
    w, mock, qr = qr_client
    await w.start_qr_login()
    qr.wait_error = TimeoutError()
    state, detail = await w.wait_qr_login()
    assert state == "error"
    assert "过期" in (detail or "")
    assert w._qr_login is None


async def test_telethon_wait_without_start_is_error(qr_client) -> None:
    w, _, _ = qr_client
    state, detail = await w.wait_qr_login()
    assert state == "error"
    assert "尚未生成" in (detail or "")


# ---- UI: 设置页编排 ----


def test_settings_has_qr_login_button(qapp, tmp_path) -> None:
    app = MagicMock()
    app.settings = Settings.for_test()
    app.client.state = "uninit"
    page = SettingsPage(app=app, loop=MagicMock(), env_path=tmp_path / ".env")
    assert page.btn_qr_login.text() == "二维码登录"
    assert "…" not in page.btn_qr_login.text()
    page.deleteLater()


def test_settings_qr_login_saves_creds_and_opens_qr_dialog(qapp, qloop, tmp_path) -> None:
    """点二维码登录:保存 api 凭据(手机号保留)→ 弹扫码框,不弹手机号框。"""
    bus = EventBus()
    client = FakeTelegramClient()
    settings = Settings.for_test(api_id=1, api_hash="x" * 32, phone="+8613800000000")
    app = MagicMock()
    app.settings = settings
    app.client = client
    app.auth = AuthService(bus=bus, client=client, settings=settings)
    app.bus = bus
    page = SettingsPage(app=app, loop=qloop, env_path=tmp_path / ".env")
    page.in_api_id.setValue(4242)
    page.in_api_hash.setText("y" * 32)
    # 手机号输入框清空 —— QR 不应因此失败,且不应该覆盖已保存的手机号

    with patch("tgmonitor.ui.widgets.qr_login_dialog.QrLoginDialog") as dlg_cls:
        page.btn_qr_login.click()
        _wait_until(lambda: dlg_cls.called)

    pairs = parse_env_file(tmp_path / ".env").pairs
    assert pairs["TG_API_ID"] == "4242"
    assert pairs["TG_API_HASH"] == "y" * 32
    assert pairs["TG_PHONE"] == "+8613800000000"  # 保留已保存值
    assert client.state == "qr_required"
    assert dlg_cls.call_args.args[2].startswith("tg://login?token=")
    page.deleteLater()


def test_settings_qr_login_bad_credentials_no_network(qapp, qloop, tmp_path) -> None:
    app = MagicMock()
    app.settings = Settings.for_test(api_id=1, api_hash="x" * 32)
    app.client.state = "uninit"
    app.auth.start_qr_login = AsyncMock()
    page = SettingsPage(app=app, loop=qloop, env_path=tmp_path / ".env")
    page.in_api_id.setValue(0)
    page.in_api_hash.setText("short")

    with patch("tgmonitor.ui.widgets.settings_page.QMessageBox.warning") as warn:
        page.btn_qr_login.click()
    warn.assert_called_once()
    app.auth.start_qr_login.assert_not_awaited()
    assert not (tmp_path / ".env").exists()
    page.deleteLater()


def test_set_login_state_hides_qr_button_when_ready(qapp, tmp_path) -> None:
    app = MagicMock()
    app.settings = Settings.for_test()
    app.client.state = "uninit"
    page = SettingsPage(app=app, loop=MagicMock(), env_path=tmp_path / ".env")
    assert page.btn_qr_login.isHidden() is False
    page.set_login_state("ready")
    assert page.btn_qr_login.isHidden() is True
    page.set_login_state("phone_required")
    assert page.btn_qr_login.isHidden() is False
    page.deleteLater()


# ---- UI: 二维码弹窗 ----


def test_make_qr_pixmap_renders_real_png() -> None:
    pix = make_qr_pixmap("tg://login?token=TEST")
    assert isinstance(pix, QPixmap)
    assert not pix.isNull()
    assert pix.width() > 0 and pix.height() > 0


def test_qr_dialog_renders_code_and_closes_on_ready(qapp, qloop) -> None:
    gate = asyncio.Event()

    async def _wait():
        await gate.wait()
        return "ready", None

    mock_app = MagicMock()
    mock_app.settings.phone = ""
    mock_app.client.state = "qr_required"
    mock_app.auth.wait_qr_login = AsyncMock(side_effect=_wait)
    mock_app.auth.start_qr_login = AsyncMock(return_value=("qr_required", "tg://login?token=X"))
    dlg = QrLoginDialog(mock_app, qloop, "tg://login?token=TEST")
    assert not dlg.qr_label.pixmap().isNull()
    assert dlg.btn_refresh.text() == "刷新二维码"
    _wait_until(lambda: mock_app.auth.wait_qr_login.await_count == 1)

    # wait 返回 ready → 主线程槽 accept()（qloop 在后台线程,须线程安全 set）
    qloop.call_soon_threadsafe(gate.set)
    _wait_until(lambda: dlg.result() == QDialog.DialogCode.Accepted)
    dlg.deleteLater()


def test_qr_dialog_refresh_cancels_old_wait(qapp, qloop) -> None:
    gate = asyncio.Event()

    async def _wait():
        await gate.wait()
        return "ready", None

    mock_app = MagicMock()
    mock_app.settings.phone = ""
    mock_app.client.state = "qr_required"
    mock_app.auth.wait_qr_login = AsyncMock(side_effect=_wait)
    mock_app.auth.start_qr_login = AsyncMock(return_value=("qr_required", "tg://login?token=NEW"))
    dlg = QrLoginDialog(mock_app, qloop, "tg://login?token=OLD")
    old_fut = dlg._wait_future
    assert old_fut is not None
    dlg.btn_refresh.click()
    assert old_fut.cancelled() or old_fut.done()
    _wait_until(lambda: mock_app.auth.start_qr_login.await_count == 1)
    dlg.close()
    dlg.deleteLater()
