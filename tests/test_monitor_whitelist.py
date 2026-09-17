"""2026-09-16 PR 5:MonitorService 白名单 / 后端热重载直测。

PR 2 plan 标 PR 5「MonitorService 8 个 0-test 私方法」。本文件覆盖最简
+ 不依赖 network / TDLib 的 3 个:`add_to_whitelist` / `remove_from_whitelist`
/ `update_backends`。其余(`_handle_pin_changed` / `_handle_channel_title_changed`
/ `_handle_channel_photo_changed` / `_maybe_store_thumb` / `_download_worker`)
需要 mock client.get_message / 下载队列,后续 PR 5 续 PR 再补。

测试目的:
- 锁住「增量 + 单摘」语义(区别 `set_whitelist` 整体替换)
- 锁住 `update_backends` 同步换 storage + objects + settings + downloader
  重建(reconfigure 路径的 fix,见 service.py:121-128 注释)
- 不连真 storage / 真 client / 真 monitor 启动 — 只测纯引用替换
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from tgmonitor.core.events import EventBus
from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.fake_client import FakeTelegramClient


def _make_monitor() -> MonitorService:
    """最小 MonitorService — 只测引用替换/集合操作,不跑网络。"""
    bus = EventBus()
    client = FakeTelegramClient()
    storage = MagicMock(spec=StorageRepository)
    objects = MagicMock()
    settings = MagicMock()
    return MonitorService(bus, client, storage, objects, settings)


# ---- add_to_whitelist / remove_from_whitelist ----


def test_add_to_whitelist_incremental() -> None:
    """`add_to_whitelist` 增量加;同一 channel 多次加幂等。"""
    mon = _make_monitor()
    assert mon._whitelist == set()

    mon.add_to_whitelist(100)
    assert mon._whitelist == {100}

    mon.add_to_whitelist(200)
    assert mon._whitelist == {100, 200}

    # 重复加不抛
    mon.add_to_whitelist(100)
    assert mon._whitelist == {100, 200}


def test_remove_from_whitelist_idempotent() -> None:
    """`remove_from_whitelist` 不存在的 channel 静默忽略,set 不抛。"""
    mon = _make_monitor()
    mon.add_to_whitelist(100)
    mon.add_to_whitelist(200)

    mon.remove_from_whitelist(100)
    assert mon._whitelist == {200}

    # 删已删过的 → 不抛,set 不变
    mon.remove_from_whitelist(100)
    assert mon._whitelist == {200}

    # 删从未加过的 → 也不抛
    mon.remove_from_whitelist(999)
    assert mon._whitelist == {200}


def test_add_remove_round_trip() -> None:
    """加完摘掉后,再加回能正常恢复(regression — set.discard 不该误删)。"""
    mon = _make_monitor()
    mon.add_to_whitelist(100)
    mon.remove_from_whitelist(100)
    mon.add_to_whitelist(100)
    assert mon._whitelist == {100}


# ---- update_backends ----


def test_update_backends_swaps_storage_objects_settings() -> None:
    """`update_backends` 把 storage / objects / settings 引用全换。"""
    mon = _make_monitor()
    new_storage = MagicMock(spec=StorageRepository)
    new_storage.list_subscribed_channels = AsyncMock(return_value=[])
    new_objects = MagicMock()
    new_settings = MagicMock()

    # 不传 downloader,内部 if 分支跳过 — 验证 storage/objects/settings 替换
    asyncio.run(mon.update_backends(new_storage, new_objects, new_settings))

    assert mon.storage is new_storage
    assert mon.objects is new_objects
    assert mon.settings is new_settings


def test_update_backends_reloads_whitelist_from_new_storage() -> None:
    """`update_backends` 从新 storage 重载白名单(订阅真理随存储切换)。

    这是 PR-#14 fix 的核心:之前 reconfigure 只换 AppService 自己的引用,
    monitor / MediaDownloader 仍持旧 storage;现 update_backends 调
    `_load_whitelist_from_storage` 把 monitor._whitelist 同步成新 storage 真理。
    """
    mon = _make_monitor()
    # 老 whitelist 应该是空的
    assert mon._whitelist == set()

    # mock 新 storage 的 list_subscribed_channels → 返 [ChannelDTO(...), ...]
    from tgmonitor.core.dto import ChannelDTO

    new_storage = MagicMock(spec=StorageRepository)
    new_storage.list_subscribed_channels = AsyncMock(
        return_value=[
            ChannelDTO(id=100, title="#100"),
            ChannelDTO(id=200, title="#200"),
        ]
    )
    new_objects = MagicMock()
    new_settings = MagicMock()

    asyncio.run(mon.update_backends(new_storage, new_objects, new_settings))

    assert mon._whitelist == {100, 200}
    new_storage.list_subscribed_channels.assert_awaited_once()


def test_update_backends_rebuilds_downloader_when_present() -> None:
    """`update_backends` 在已有 downloader 时重建一个(新 storage/objects 引用)。"""
    mon = _make_monitor()
    # mock 旧 downloader,update_backends 应替换它
    old_downloader = MagicMock()
    mon.downloader = old_downloader

    new_storage = MagicMock(spec=StorageRepository)
    new_storage.list_subscribed_channels = AsyncMock(return_value=[])
    new_objects = MagicMock()
    new_settings = MagicMock()

    asyncio.run(mon.update_backends(new_storage, new_objects, new_settings))

    # 引用换了,新 downloader 是 MediaDownloader 实例
    from tgmonitor.core.monitor.service import MediaDownloader

    assert mon.downloader is not old_downloader
    assert isinstance(mon.downloader, MediaDownloader)
    # 新 downloader 应绑新 storage / objects
    assert mon.downloader.storage is new_storage
    assert mon.downloader.objects is new_objects
