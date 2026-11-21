"""AuthService 单测 — 二维码登录编排 / 凭据预检 / 错误路径。

2026-11-21:手机号+验证码登录链移除后,登录方式只有二维码扫码。
覆盖:
- 凭据预检:api_id / api_hash 必填
- 委托语义:start_qr_login / wait_qr_login 转给 client
- 错误路径:client 抛异常 → 返回 ('error', msg) + 发 ErrorOccurred
- 登录态查询 / 登出透传
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tgmonitor.core.auth_service import AuthService
from tgmonitor.core.events import ErrorOccurred, EventBus
from tgmonitor.core.telegram.fake_client import FakeTelegramClient

pytestmark = pytest.mark.asyncio


@pytest.fixture
def auth_service(settings):
    bus = EventBus()
    client = FakeTelegramClient()
    return AuthService(bus=bus, client=client, settings=settings), bus


async def test_start_qr_login_delegates_to_client(auth_service) -> None:
    auth, _ = auth_service
    state, url = await auth.start_qr_login()
    assert state == "qr_required"
    assert url == "tg://login?token=fake-test-token"


async def test_wait_qr_login_delegates_to_client(auth_service) -> None:
    auth, _ = auth_service
    state, _ = await auth.start_qr_login()
    assert state == "qr_required"
    state, _ = await auth.wait_qr_login()
    assert state == "ready"


async def test_start_qr_login_without_api_id_returns_error(auth_service) -> None:
    auth, bus = auth_service
    seen: list[ErrorOccurred] = []
    bus.subscribe(ErrorOccurred, lambda e: seen.append(e))
    auth._settings.api_id = 0
    state, detail = await auth.start_qr_login()
    assert state == "error"
    assert "TG_API_ID" in detail
    assert any(s.source == "start_qr_login" for s in seen)


async def test_start_qr_login_without_api_hash_returns_error(auth_service) -> None:
    auth, _ = auth_service
    auth._settings.api_hash = ""
    state, detail = await auth.start_qr_login()
    assert state == "error"
    assert "TG_API_HASH" in detail


async def test_qr_login_does_not_require_phone(auth_service) -> None:
    """二维码登录不校验手机号 —— 没填手机号也能生成二维码。"""
    auth, _ = auth_service
    state, _ = await auth.start_qr_login()
    assert state == "qr_required"


async def test_start_qr_login_client_exception_returns_error(auth_service) -> None:
    auth, bus = auth_service
    auth._client.start_qr_login = AsyncMock(side_effect=RuntimeError("net"))  # type: ignore[method-assign]
    seen: list[ErrorOccurred] = []
    bus.subscribe(ErrorOccurred, lambda e: seen.append(e))
    state, detail = await auth.start_qr_login()
    assert state == "error"
    assert "net" in detail
    assert any(s.source == "start_qr_login" for s in seen)


async def test_wait_qr_login_client_exception_returns_error(auth_service) -> None:
    auth, _ = auth_service
    auth._client.wait_qr_login = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    state, detail = await auth.wait_qr_login()
    assert state == "error"
    assert "boom" in detail


async def test_get_login_state_transparent(auth_service) -> None:
    auth, _ = auth_service
    assert await auth.get_login_state() == "phone_required"


async def test_logout_delegates_to_client(auth_service) -> None:
    auth, _ = auth_service
    auth._client.logout = AsyncMock()  # type: ignore[method-assign]
    await auth.logout()
    auth._client.logout.assert_awaited_once()  # type: ignore[attr-defined]
