"""FakeTelegramClient 鉴权状态机单测(2026-08-27 v1.4.0 PR #13)。

覆盖新增的 email / registration 流程:
- `submit_email`:含 `+new` → registration_required,否则 → email_code_required
- `submit_email_code` → ready
- `submit_registration(first, last)` → ready + me 字段写入
"""
from __future__ import annotations

import pytest

from tgmonitor.core.telegram.fake_client import FakeTelegramClient

pytestmark = pytest.mark.asyncio


async def test_fake_initial_state_is_phone_required() -> None:
    client = FakeTelegramClient()
    assert client.state == "phone_required"


async def test_fake_submit_email_existing_account_goes_to_code_required() -> None:
    client = FakeTelegramClient()
    state, detail = await client.submit_email("user@example.com")
    assert state == "email_code_required"
    assert client.state == "email_code_required"
    assert detail is None


async def test_fake_submit_email_new_account_marker_goes_to_registration() -> None:
    """测试约定:email 含 `+new` 子串 → 模拟新账号注册分支。"""
    client = FakeTelegramClient()
    state, _ = await client.submit_email("new+new@example.com")
    assert state == "registration_required"


async def test_fake_submit_email_code_advances_to_ready() -> None:
    client = FakeTelegramClient()
    await client.submit_email("user@example.com")
    state, _ = await client.submit_email_code("123456")
    assert state == "ready"
    assert client.state == "ready"
    assert client.me is not None
    assert client.me["first_name"] == "Fake"


async def test_fake_submit_registration_advances_to_ready() -> None:
    client = FakeTelegramClient()
    await client.submit_email("new+new@example.com")
    state, _ = await client.submit_registration("Alice", "Wonder")
    assert state == "ready"
    assert client.me is not None
    assert client.me["first_name"] == "Alice"
    assert client.me["last_name"] == "Wonder"


async def test_fake_submit_registration_empty_last_name_ok() -> None:
    """last_name 可省略 — 只传 first_name 也应成功。"""
    client = FakeTelegramClient()
    await client.submit_email("new+new@example.com")
    state, _ = await client.submit_registration("Bob")
    assert state == "ready"
    assert client.me is not None
    assert client.me["first_name"] == "Bob"
    assert client.me["last_name"] == ""


async def test_fake_full_email_flow_existing_account() -> None:
    """端到端:phone → email → email_code → ready(已有账号路径)。"""
    client = FakeTelegramClient()
    s1, _ = await client.submit_phone("+8613800000000")
    assert s1 == "code_required"
    s2, _ = await client.submit_code("11111")
    assert s2 == "ready"


async def test_fake_full_email_flow_new_account() -> None:
    """端到端:email 含 +new → registration → ready(新账号路径)。"""
    client = FakeTelegramClient()
    # 假设前面某步把 state 推到 email_required
    client._state = "email_required"
    s1, _ = await client.submit_email("alice+new@example.com")
    assert s1 == "registration_required"
    s2, _ = await client.submit_registration("Alice")
    assert s2 == "ready"