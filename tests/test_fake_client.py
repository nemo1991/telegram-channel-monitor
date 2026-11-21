"""FakeTelegramClient 鉴权状态机单测 — 二维码登录(本版本唯一登录方式)。

- 初始 state = phone_required(未登录通用占位)
- `start_qr_login` → qr_required + token URL
- `wait_qr_login` → ready + me 字段写入
- `logout` → 回 phone_required
"""

from __future__ import annotations

import pytest

from tgmonitor.core.telegram.fake_client import FakeTelegramClient

pytestmark = pytest.mark.asyncio


async def test_fake_initial_state_is_phone_required() -> None:
    client = FakeTelegramClient()
    assert client.state == "phone_required"


async def test_fake_qr_login_goes_to_qr_required_then_ready() -> None:
    client = FakeTelegramClient()
    state, url = await client.start_qr_login()
    assert state == "qr_required"
    assert url and "token=" in url

    state, _ = await client.wait_qr_login()
    assert state == "ready"
    assert client.state == "ready"
    assert client.me is not None
    assert client.me["first_name"] == "Fake"


async def test_fake_logout_resets_to_phone_required() -> None:
    client = FakeTelegramClient()
    await client.start_qr_login()
    await client.wait_qr_login()
    await client.logout()
    assert client.state == "phone_required"
    assert client.me is None
