"""2026-09-17 PR 4 mega-split:`AppService` 非-batch 直测(login / sub / lifecycle)。

从原 `tests/test_monitor_and_app.py` 拆出。2026-11-11 v2.0.0 Phase 3-C2:
facade 域转发删除后,list_messages / search 的 4 个「mock 自证」转发测试
一并删除 — 真行为契约由 `test_message_service.py`(storage 级)覆盖。

6 tests:
- login state machine(2):get_login_state 透传 client.state / 未填凭证 login 抛错
- subscribe / unsubscribe(3):正常路径 + storage 抛错时 unsubscribe 不假成功 /
  失败不发 ErrorOccurred
- start_monitor(1):不留 _subscribed 旧 attr
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from tgmonitor.core.app_service import AppService
from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import ErrorOccurred


async def test_app_login_state_machine(bus) -> None:
    """AppService.get_login_state 直接透传 client.state。

    State machine 真相在 client;facade 仅暴露读接口,不缓存。
    """
    from tests.fixtures._app_service_batch import _make_app

    svc = await _make_app(bus, client_state="phone_required")
    assert await svc.auth.get_login_state() == "phone_required"
    # FakeTelegramClient.state 是 property(无 setter);通过 _state 改底层
    svc.client._state = "ready"  # type: ignore[attr-defined]
    assert await svc.auth.get_login_state() == "ready"


async def test_app_login_without_credentials_raises(bus) -> None:
    """bootstrap 时 client.start() 报"未配置凭据"→ bootstrap 返 ("phone_required",
    "未配置…"),**不**抛 — AuthService 期望从该 detail 引导用户填表。

    这是 UnconfiguredClient 的契约:鉴权 facade 不应该冒泡,UI 应能
    `state == "phone_required"` 走引导流程。
    """
    from tests.fixtures._app_service_batch import _make_unconfigured_app

    svc = await _make_unconfigured_app(bus)
    state, detail = await svc.bootstrap()
    assert state == "phone_required"
    assert detail is not None
    assert "未配置" in detail or "请打开" in detail or "凭证" in detail


async def test_app_subscribe_unsubscribe(batch_app: AppService, collected: list) -> None:
    """subscribe_channel → channels → storage.upsert_channel + set_channel_subscribed(True);
    unsubscribe_channel → storage.set_channel_subscribed(False) — 2026-07-31
    修复后走 set_channel_subscribed 简化路径。

    回归:v1.5.0 PR #A2 把 facade 拆 3 个子 service,本测试确保 subscribe /
    unsubscribe 没绕过 channels 直调。
    """
    chan = ChannelDTO(id=42, title="test")
    await batch_app.channels.subscribe_channel(chan)
    # subscribe 路径:upsert_channel + set_channel_subscribed(True) 均被调
    batch_app.channels._storage.upsert_channel.assert_awaited_once_with(chan)  # type: ignore[attr-defined]
    batch_app.channels._storage.set_channel_subscribed.assert_awaited_with(42, True)  # type: ignore[attr-defined]
    await batch_app.channels.unsubscribe_channel(42)
    # unsubscribe 路径:set_channel_subscribed(42, False)
    batch_app.channels._storage.set_channel_subscribed.assert_awaited_with(42, False)  # type: ignore[attr-defined]
    # 正常路径不发 ErrorOccurred
    errs = [e for e in collected if isinstance(e, ErrorOccurred)]
    assert errs == []


async def test_unsubscribe_storage_failure_raises_no_false_success(
    batch_app: AppService,
) -> None:
    """unsubscribe 时 storage.set_channel_subscribed 抛错 → exception 冒泡,
    不留 partial 状态。

    旧 facade 有 catch-all,UI 看到 success 但 storage 没改(SUBSCRIBED_DRIFT_ANALYSIS #A)。
    """
    batch_app.channels._storage.set_channel_subscribed = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("storage down")
    )
    with pytest.raises(RuntimeError, match="storage down"):
        await batch_app.channels.unsubscribe_channel(42)


async def test_unsubscribe_failure_does_not_publish_event(batch_app: AppService, bus) -> None:
    """unsubscribe 失败 → 不发 ErrorOccurred(让上层处理异常,不污染事件流)。

    UI 弹错对话框依据事件订阅,若错误事件被发,UI 弹两次(异常一次 + 事件一次)。
    """
    batch_app.channels._storage.set_channel_subscribed = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("simulated")
    )
    seen: list = []

    async def _on(e):
        seen.append(e)

    bus.subscribe(ErrorOccurred, _on)
    try:
        with pytest.raises(RuntimeError):
            await batch_app.channels.unsubscribe_channel(42)
        await asyncio.sleep(0)
        # 不应再有 ErrorOccurred 事件
        assert all(not isinstance(e, ErrorOccurred) for e in seen)
    finally:
        bus.unsubscribe(ErrorOccurred, _on)


async def test_no_subscribed_attribute_remains(batch_app: AppService) -> None:
    """start_monitor / stop_monitor 不留下 _subscribed 旧私有 attr。

    回归:v1.5.0 拆 facade 时留了 _subscribed 占位,后续 PR 删了它但忘了在测试
    里锁定。本测试确保该 attr 永远不被重建(若有说明回退)。
    """
    await batch_app.start_monitor()
    # 仅在 facade 显式管理时才算合规;若残留说明历史 cache 没删干净
    val = getattr(batch_app, "_subscribed", "__missing__")
    assert val in (None, False, "__missing__"), (
        f"AppService 不应有 _subscribed 残留(已删 cache);实际值 = {val!r}"
    )
    await batch_app.stop_monitor()
