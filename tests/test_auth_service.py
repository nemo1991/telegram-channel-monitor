"""AuthService 单测(2026-08-27 v1.4.0 PR #13)。

覆盖 email / registration 流程的:
- 输入校验:邮箱格式、first_name 非空
- 委托语义:把请求转给 `TelegramClient.submit_*`
- 错误路径:client 抛异常 → 返回 `('error', msg)` + 发 `ErrorOccurred`
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


async def test_submit_email_valid(auth_service) -> None:
    auth, bus = auth_service
    state, _ = await auth.submit_email("user@example.com")
    # Fake client:含 `+new` 才会推到 registration,普通邮箱 → email_code_required
    assert state == "email_code_required"


async def test_submit_email_invalid_format_returns_error(auth_service) -> None:
    auth, bus = auth_service
    seen: list[ErrorOccurred] = []
    bus.subscribe(ErrorOccurred, lambda e: seen.append(e))
    state, detail = await auth.submit_email("not-an-email")
    assert state == "error"
    assert "邮箱" in detail
    # ErrorOccurred 已发布
    assert any(s.source == "submit_email" for s in seen)


async def test_submit_email_too_long_returns_error(auth_service) -> None:
    auth, _ = auth_service
    state, detail = await auth.submit_email("a@" + "x" * 100)
    assert state == "error"
    assert "邮箱" in detail


async def test_submit_email_code_empty_returns_error(auth_service) -> None:
    auth, _ = auth_service
    state, detail = await auth.submit_email_code("")
    assert state == "error"
    assert "验证码" in detail


async def test_submit_email_code_delegates_to_client(auth_service) -> None:
    auth, _ = auth_service
    # 让 client.submit_email_code 抛 — 验证 _fail 路径
    auth._client.submit_email_code = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("boom"),
    )
    state, detail = await auth.submit_email_code("123456")
    assert state == "error"
    assert "boom" in detail


async def test_submit_registration_empty_first_name_returns_error(auth_service) -> None:
    auth, _ = auth_service
    state, detail = await auth.submit_registration("")
    assert state == "error"
    assert "first_name" in detail


async def test_submit_registration_delegates_with_args(auth_service) -> None:
    auth, _ = auth_service
    captured: dict = {}

    async def fake_reg(first: str, last: str):
        captured["first"] = first
        captured["last"] = last
        return "ready", None

    auth._client.submit_registration = fake_reg  # type: ignore[method-assign]
    state, _ = await auth.submit_registration("Alice", "Wonder")
    assert state == "ready"
    assert captured == {"first": "Alice", "last": "Wonder"}


async def test_submit_email_client_exception_returns_error(auth_service) -> None:
    auth, bus = auth_service
    auth._client.submit_email = AsyncMock(side_effect=RuntimeError("net"))  # type: ignore[method-assign]
    seen: list[ErrorOccurred] = []
    bus.subscribe(ErrorOccurred, lambda e: seen.append(e))
    state, detail = await auth.submit_email("user@example.com")
    assert state == "error"
    assert "net" in detail
    assert any(s.source == "submit_email" for s in seen)


async def test_submit_phone_uses_passed_phone_not_saved_settings(auth_service) -> None:
    """2.1.1:设置页填的手机号未保存进 settings 时仍能发出验证码。

    登录入口走「填了就能登录」,校验必须用当场提交的 phone;此前用已保存的
    settings.phone(= "")校验会误判未配置,根本不调用 client 发验证码。
    """
    auth, _ = auth_service
    auth._settings.phone = ""  # 模拟设置页刚填、尚未保存
    state, _ = await auth.submit_phone("+8613800000000")
    assert state == "code_required"


async def test_submit_phone_with_empty_passed_phone_still_rejected(auth_service) -> None:
    """边界:传入空手机号仍被拦截(不绕过校验)。"""
    auth, _ = auth_service
    auth._settings.phone = "+8613900000000"  # 已保存一个合法号
    state, detail = await auth.submit_phone("")
    assert state == "error"
    assert "TG_PHONE" in detail
