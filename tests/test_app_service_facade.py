"""PR #A2 — AppService 拆分后,验证 facade 真的 1:1 转发到子 service。

这是「contract test」:即便 facade 实现方式换了(子 service swap / 直接
转发),子 service 必须收到正确参数。这层保证后续重构(再拆 / 合并 /
改名)有兜底。

约定:
  - 不测内部实现细节(如 `app._media` 字段名)
  - 只测「facade.<method> → 子 service.<method> 真被调 + 参数一致」
  - 子 service 真实类型不被断言(避免重构时全坏)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tgmonitor.core.app_service import AppService
from tgmonitor.core.events import EventBus


@pytest.fixture
async def app() -> AppService:
    """最小可用的 AppService — 不接 monitor / channel_sync。

    用 AsyncMock 模拟 client / storage / objects,只验证 facade 转发的
    协议面正确(真业务逻辑由既有 test_*.py 覆盖)。
    """
    bus = EventBus()
    client = AsyncMock()
    client.state = "phone_required"
    storage = AsyncMock()
    objects = AsyncMock()
    objects.backend_name = "fake"
    settings = MagicMock()
    settings.media_policy = "full"
    return AppService(bus, client, storage, objects, settings)


def _find_sub_service(app: AppService, method_name: str):
    """找具有 method_name 方法的属性(排除 AsyncMock 自身)。

    vars(app) 含所有属性,但 storage/objects/client/bus 是 AsyncMock 也
    有 list_media 等属性(继承 mock)。需要过滤「构造 AppService 时注入的
    引用」vs「构造时新建的子 service」。
    """
    expected_classes = {"SubscriptionService", "MediaService"}
    return next(
        (
            v
            for v in vars(app).values()
            if type(v).__name__ in expected_classes and hasattr(v, method_name)
        ),
        None,
    )


@pytest.mark.asyncio
async def test_facade_forwards_list_media_to_media_service(app: AppService) -> None:
    """`app.list_media` 必须真调到子 service(模拟),不走 storage 直调。

    验证:facade.list_media 返回 mock 的值(不是真查 storage 的结果),
    证明 facade 没绕过子 service 自己实现。
    """
    expected = ([(MagicMock(), 0, MagicMock())], 42)
    media_svc = _find_sub_service(app, "list_media")
    assert media_svc is not None, "未找到具有 list_media 方法的子 service"
    media_svc.list_media = AsyncMock(return_value=expected)  # type: ignore[attr-defined]

    result = await app.list_media(channel_id=100, limit=50)
    media_svc.list_media.assert_awaited_once()  # type: ignore[attr-defined]
    kwargs = media_svc.list_media.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["channel_id"] == 100
    assert kwargs["limit"] == 50
    assert result is expected


@pytest.mark.asyncio
async def test_facade_forwards_list_messages_to_subscription_service(app: AppService) -> None:
    """`app.list_messages` 必须真调到子 service(模拟)。"""
    expected = [MagicMock()]
    sub_svc = _find_sub_service(app, "list_messages")
    assert sub_svc is not None, "未找到具有 list_messages 方法的子 service"
    sub_svc.list_messages = AsyncMock(return_value=expected)  # type: ignore[attr-defined]

    result = await app.list_messages(channel_ids=[1, 2], limit=10)
    sub_svc.list_messages.assert_awaited_once_with(  # type: ignore[attr-defined]
        [1, 2],
        None,
        None,
        10,
        search="",
    )
    assert result is expected


@pytest.mark.asyncio
async def test_facade_forwards_list_messages_search_kwarg(app: AppService) -> None:
    """PR #B2:`app.list_messages(search="foo")` 把 search 透传给子 service。"""
    expected = [MagicMock()]
    sub_svc = _find_sub_service(app, "list_messages")
    assert sub_svc is not None
    sub_svc.list_messages = AsyncMock(return_value=expected)  # type: ignore[attr-defined]

    result = await app.list_messages(channel_ids=[1], search="hello")
    sub_svc.list_messages.assert_awaited_once_with(  # type: ignore[attr-defined]
        [1],
        None,
        None,
        200,
        search="hello",
    )
    assert result is expected


@pytest.mark.asyncio
async def test_facade_objects_setter_syncs_to_media_service(app: AppService) -> None:
    """`app.objects = X` 必须同步给 MediaService(否则 reconcile_orphans 等
    仍按旧 backend 分支走)。

    触发场景:
      - 测试 monkeypatch swap
      - reconfigure 末尾 `_rebuild_objects` 写入
    """
    new_objects = MagicMock()
    new_objects.backend_name = "fake"
    app.objects = new_objects  # type: ignore[assignment]
    media_svc = _find_sub_service(app, "list_media")
    assert media_svc is not None
    assert media_svc._objects is new_objects  # noqa: SLF001 — explicit sync contract


@pytest.mark.asyncio
async def test_facade_storage_setter_syncs_to_both_sub_services(app: AppService) -> None:
    """`app.storage = X` 必须同步给 SubscriptionService + MediaService。

    触发场景:
      - 测试 monkeypatch swap
      - reconfigure 末尾 `_rebuild_storage` 写入
    """
    new_storage = MagicMock()
    app.storage = new_storage  # type: ignore[assignment]
    for attr, expected_cls in (
        ("list_messages", "SubscriptionService"),
        ("list_media", "MediaService"),
    ):
        sub_svc = next(
            (
                v
                for v in vars(app).values()
                if type(v).__name__ == expected_cls and hasattr(v, attr)
            ),
            None,
        )
        assert sub_svc is not None, f"未找到 {expected_cls} 子 service"
        assert sub_svc._storage is new_storage  # noqa: SLF001
