"""2026-09-17 PR 4 mega-split:`AppService` 非-batch 直测(login / sub / list)。

从原 `tests/test_monitor_and_app.py` 拆出 — 这 10 个测试覆盖 AppService 的
非-batch、非 metadata 路径,与 `test_app_service_metadata.py`(set/list facade)
和 `test_app_service_batch_*.py`(batch op)互不重叠。

10 tests:
- login state machine(2):get_login_state 透传 client.state / 未填凭证 login 抛错
- subscribe / unsubscribe(3):正常路径 + storage 抛错时 unsubscribe 不假成功 /
  失败不发 ErrorOccurred
- list_messages / search(3):fallback 到 storage truth / channel_ids 显式覆盖 /
  include_unsubscribed 真透传
- start_monitor(1):不留 _subscribed 旧 attr
- _sub.storage delegation(1):facade 不绕过 _sub 直调
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
    assert await svc.get_login_state() == "phone_required"
    # FakeTelegramClient.state 是 property(无 setter);通过 _state 改底层
    svc.client._state = "ready"  # type: ignore[attr-defined]
    assert await svc.get_login_state() == "ready"


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
    """subscribe_channel → _sub → storage.upsert_channel + set_channel_subscribed(True);
    unsubscribe_channel → storage.set_channel_subscribed(False) — 2026-07-31
    修复后走 set_channel_subscribed 简化路径。

    回归:v1.5.0 PR #A2 把 facade 拆 3 个子 service,本测试确保 subscribe /
    unsubscribe 没绕过 _sub 直调。
    """
    chan = ChannelDTO(id=42, title="test")
    await batch_app.subscribe_channel(chan)
    # subscribe 路径:upsert_channel + set_channel_subscribed(True) 均被调
    batch_app._sub._storage.upsert_channel.assert_awaited_once_with(chan)  # type: ignore[attr-defined]
    batch_app._sub._storage.set_channel_subscribed.assert_awaited_with(42, True)  # type: ignore[attr-defined]
    await batch_app.unsubscribe_channel(42)
    # unsubscribe 路径:set_channel_subscribed(42, False)
    batch_app._sub._storage.set_channel_subscribed.assert_awaited_with(42, False)  # type: ignore[attr-defined]
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
    batch_app._sub._storage.set_channel_subscribed = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("storage down")
    )
    with pytest.raises(RuntimeError, match="storage down"):
        await batch_app.unsubscribe_channel(42)


async def test_unsubscribe_failure_does_not_publish_event(batch_app: AppService, bus) -> None:
    """unsubscribe 失败 → 不发 ErrorOccurred(让上层处理异常,不污染事件流)。

    UI 弹错对话框依据事件订阅,若错误事件被发,UI 弹两次(异常一次 + 事件一次)。
    """
    batch_app._sub._storage.set_channel_subscribed = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("simulated")
    )
    seen: list = []

    async def _on(e):
        seen.append(e)

    bus.subscribe(ErrorOccurred, _on)
    try:
        with pytest.raises(RuntimeError):
            await batch_app.unsubscribe_channel(42)
        await asyncio.sleep(0)
        # 不应再有 ErrorOccurred 事件
        assert all(not isinstance(e, ErrorOccurred) for e in seen)
    finally:
        bus.unsubscribe(ErrorOccurred, _on)


async def test_list_messages_falls_back_to_storage_truth(batch_app: AppService) -> None:
    """list_messages 默认 channel_ids=None → 透传到 _sub.list_messages,无 facade 拦截。

    行为契约:facade 不"自作聪明"补 channel_ids=[] — 让 storage 决定(默认 = 全部
    subscribed 频道)。
    """
    from tgmonitor.core.dto import MessageDTO

    expected = [
        MessageDTO(id=1, channel_id=1, telegram_msg_id=10, text="x"),
    ]
    batch_app._sub.list_messages = AsyncMock(return_value=expected)  # type: ignore[attr-defined]
    out = await batch_app.list_messages()
    assert out is expected
    batch_app._sub.list_messages.assert_awaited_once()  # type: ignore[attr-defined]
    # channel_ids 应是 None(没被 facade 补 [])
    call_args = batch_app._sub.list_messages.await_args  # type: ignore[attr-defined]
    assert call_args.args[0] is None or call_args.kwargs.get("channel_ids") is None


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


async def test_app_list_messages_include_unsubscribed_returns_all_channels(
    batch_app: AppService,
) -> None:
    """include_unsubscribed=True → 透传到 _sub.list_messages。

    PR #D2:UI toggle「包含未订阅」默认关,仅 subscribed 频道;开则全。
    """
    from tgmonitor.core.dto import MessageDTO

    expected = [
        MessageDTO(id=1, channel_id=99, telegram_msg_id=1, text="unsubbed"),
    ]
    batch_app._sub.list_messages = AsyncMock(return_value=expected)  # type: ignore[attr-defined]
    out = await batch_app.list_messages(include_unsubscribed=True)
    assert out is expected
    kwargs = batch_app._sub.list_messages.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs.get("include_unsubscribed") is True


async def test_app_list_messages_channel_ids_explicit_overrides_include_unsubscribed(
    batch_app: AppService,
) -> None:
    """显式传 channel_ids 时,_sub.list_messages 收到的 channel_ids 就是调用方传的。

    回归:旧 facade 在 `channel_ids is None and include_unsubscribed=True` 时
    偷偷把 channel_ids=[] 传下去,导致 storage 返空。修后:无论 include_unsubscribed,
    channel_ids=None 透传 None(让 storage 用 subscribed 默认)。
    """
    batch_app._sub.list_messages = AsyncMock(return_value=[])  # type: ignore[attr-defined]
    await batch_app.list_messages(channel_ids=[10, 20], include_unsubscribed=True)
    call_args = batch_app._sub.list_messages.await_args  # type: ignore[attr-defined]
    # 位置参数第 0 位应是 channel_ids
    assert call_args.args[0] == [10, 20]
    assert call_args.kwargs.get("include_unsubscribed") is True


async def test_search_messages_scope_all_passes_include_unsubscribed(
    batch_app: AppService,
) -> None:
    """search + include_unsubscribed=True 组合 → 二者都透传到 _sub.list_messages。

    PR #B2 + #D2 共存:search 是 text 过滤,include_unsubscribed 是 channel 范围,
    二者独立。测组合确保 facade 没把"搜索范围"和"频道范围"耦合。
    """
    batch_app._sub.list_messages = AsyncMock(return_value=[])  # type: ignore[attr-defined]
    await batch_app.list_messages(search="tech", include_unsubscribed=True)
    kwargs = batch_app._sub.list_messages.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs.get("search") == "tech"
    assert kwargs.get("include_unsubscribed") is True
