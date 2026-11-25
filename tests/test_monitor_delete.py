"""2026-09-17 PR 4 mega-split:MonitorService 消息删除相关测试。

10 tests:
- delete_message 删孤儿 bytes(2):refcount=0 真删 / refcount>0 保留
- UI replace_message(1):编辑事件触发 MessageView 局部刷新
- MessageDeleted handler(4):row 删除 / 异常吞掉 / 不 republish / 清孤儿 bytes
- delete_messages + cancel_event(2):cancel 中段 / 不传 cancel 全删

helper `_media_with_file_id` 在 tests/test_monitor_media.py 也用 — 本文件独立
重新定义(避免跨文件 import;helper 重复成本低)。
"""

from __future__ import annotations

import asyncio
import dataclasses

from tests.conftest import make_message
from tgmonitor.core.dto import (
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
)
from tgmonitor.core.monitor.service import MonitorService


def _media_with_file_id(file_id: str, **overrides) -> MediaDTO:
    """构造带 telegram_file_id 的 MediaDTO(下载队列用)。"""
    return MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name="pic.jpg",
        file_size=11,
        telegram_file_id=file_id,
        **overrides,
    )


async def test_delete_message_removes_orphan_bytes(
    bus, storage, objectstore, settings, client
) -> None:
    """2026-08-24 delete_message 清孤儿 bytes:唯一引用该 key 的 message 被删
    → refcount=0 → objects.delete 被调 → ObjectStore 里 key 不再存在。

    路径:storage 里一条 message 带 fid="fid-X" + object_key="media/abc.png"
    → delete_message(100, 1) → ObjectStore 上 "media/abc.png" 被真删。
    """
    # 落库 + 写入 ObjectStore
    await objectstore.put("media/abc.png", b"image-bytes", None)
    assert await objectstore.exists("media/abc.png")
    base = _media_with_file_id("fid-X")
    done = dataclasses.replace(
        base,
        object_key="media/abc.png",
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
        file_size=len(b"image-bytes"),
    )
    msg = make_message(channel_id=100, msg_id=1, text="", media=[done])
    await storage.save_message(msg)
    # 跑 MonitorService.delete_message
    mon = MonitorService(bus, client, storage, objectstore, settings)
    await mon.delete_message(100, 1)
    # message 已删
    assert await storage.get_message(100, 1) is None
    # refcount=0 → bytes 真删
    assert not await objectstore.exists("media/abc.png")


# ============================================================
# 2026-08-27 v1.4.0 PR #11:`MessageDeleted` 路由 → monitor 删 row + 清孤儿
# bytes。`delete_message` 已存在(用户主动删);新增 `_handle_message_deleted`
# 订阅总线事件,语义同但**不** republish(避免无限循环)。
# ============================================================


# ============================================================
# 2026-09-11 v1.7.4 regression:MonitorService.delete_messages cancel_event 支持
# ============================================================


async def test_monitor_delete_messages_supports_cancel_event(monitor, storage, bus) -> None:
    """回归修 #3a:`monitor.delete_messages(items, cancel_event=...)` 在 cancel_event
    set 后 break,只删到 cancel 之前成功的项,后面的 items 不动。

    旧 facade 一次性调 monitor.delete_messages(items) 不可中断;现在支持
    可选 cancel_event 让 facade 可中途 break。
    """
    # 预存 5 条消息
    for mid in range(1, 6):
        await storage.save_message(
            make_message(channel_id=100, msg_id=mid, text=f"msg-{mid}"),
        )
    # 用真 cancel_event,在删了 2 条后 set
    cancel = asyncio.Event()

    # 包装 _delete_with_orphan_check,记录被调次数 + 设 cancel
    deleted_calls: list[tuple[int, int]] = []

    orig = monitor._delete_with_orphan_check  # type: ignore[attr-defined]

    async def _counting_delete(cid: int, mid: int) -> None:
        deleted_calls.append((cid, mid))
        if len(deleted_calls) >= 2:
            cancel.set()
        await orig(cid, mid)

    monitor._delete_with_orphan_check = _counting_delete  # type: ignore[attr-defined]

    items = [(100, m) for m in range(1, 6)]
    succeeded = await monitor.delete_messages(items, cancel_event=cancel)

    # 只删了 2 条,后 3 条不应被调用
    assert succeeded == 2
    assert deleted_calls == [(100, 1), (100, 2)]

    # 后 3 条仍存在
    for mid in range(3, 6):
        m = await storage.get_message(100, mid)
        assert m is not None, f"msg-{mid} 不应被删"
