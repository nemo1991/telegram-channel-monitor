"""AppService 组合根接线 contract test。

2026-11-11 v2.0.0 Phase 3-C2:41 个 facade 域转发已删除,UI/CLI 直调
`app.<domain>.<method>`。本文件不再测转发,改测组合根保留的接线契约:

  - backend swap 传播:`app.objects/storage = X` setter 必须同步到子 service
  - `subscribe_updates` 直接代理 client 并登记 stream
  - `shutdown` 分阶段超时(client hang 不阻塞 storage/objects 关闭)

公开表面本身由 `tests/test_app_service_surface.py` allowlist 锁死。
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
    expected_classes = {"ChannelService", "MessageService", "MediaService", "BatchService"}
    return next(
        (
            v
            for v in vars(app).values()
            if type(v).__name__ in expected_classes and hasattr(v, method_name)
        ),
        None,
    )


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


async def test_facade_storage_setter_syncs_to_sub_services(app: AppService) -> None:
    """`app.storage = X` 必须同步给 ChannelService + MessageService + MediaService。

    触发场景:
      - 测试 monkeypatch swap
      - reconfigure 末尾 `_rebuild_storage` 写入
    """
    new_storage = MagicMock()
    app.storage = new_storage  # type: ignore[assignment]
    for attr, expected_cls in (
        ("list_subscribed_channels", "ChannelService"),
        ("list_messages", "MessageService"),
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


# ---- subscribe_updates(2026-11-11 Phase 3-C1:从 SubscriptionService 收回 facade)----


async def test_facade_subscribe_updates_proxies_client(app: AppService) -> None:
    """`subscribe_updates` 是 sync 方法,直接转 client.subscribe_updates 并登记 stream。"""
    sentinel = MagicMock()
    # client 是无 spec 的 AsyncMock — subscribe_updates 默认也是 async mock,
    # 显式换成 sync MagicMock(Protocol 里它是 sync 方法)。
    app.client.subscribe_updates = MagicMock(return_value=sentinel)

    result = app.subscribe_updates()

    assert result is sentinel
    app.client.subscribe_updates.assert_called_once_with()


# ---- shutdown 分阶段超时 (2026-09-22 v1.8.x) ----------------------------------


async def test_shutdown_client_close_timeout_does_not_block(app: AppService) -> None:
    """v1.8.x:`shutdown()` 给 `client.close()` 包 2s hard timeout。client 永远
    hang 不上抛,`shutdown()` 仍能在 ~2.1s 内返回(stage 之后 `storage.close()` /
    `objects.close()` 仍跑)。

    之前整链共 5s 外层 wait_for,client 一卡整链被强 cancel → TDLib 子进程
    未 join → 下次启动 401 → nuke_and_rebuild 重建 session → 用户被踢回
    登录。修复后 client 超时只 log error,继续往下关 storage / objects。
    """
    import asyncio as _asyncio

    started = _asyncio.get_event_loop().time()

    # 1) mock client.close() 永久 hang
    async def hanging_close() -> None:
        await _asyncio.Event().wait()  # 永远不返回

    app.client.close = hanging_close  # type: ignore[method-assign]
    # 2) mock storage/objects close,记录被调过
    storage_close_called = _asyncio.Event()
    objects_close_called = _asyncio.Event()

    async def fast_storage_close() -> None:
        storage_close_called.set()

    async def fast_objects_close() -> None:
        objects_close_called.set()

    app.storage.close = fast_storage_close  # type: ignore[method-assign]
    app.objects.close = fast_objects_close  # type: ignore[method-assign]

    # 3) shutdown 应在 ~2s 后返回(stage 之后仍走)
    await app.shutdown()
    elapsed = _asyncio.get_event_loop().time() - started
    assert 1.5 < elapsed < 4.0, f"shutdown 应在 2s timeout 后返回,实跑 {elapsed:.2f}s"
    assert storage_close_called.is_set(), "client timeout 后 storage.close() 仍应被调"
    assert objects_close_called.is_set(), "client timeout 后 objects.close() 仍应被调"
