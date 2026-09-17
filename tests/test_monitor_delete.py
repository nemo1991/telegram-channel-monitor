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
import os
from datetime import UTC, datetime

from tests.conftest import make_message
from tgmonitor.core.dto import (
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
)
from tgmonitor.core.events import MessageDeleted
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


async def test_delete_message_keeps_bytes_when_referenced(
    bus, storage, objectstore, settings, client
) -> None:
    """同 file_id 两条 message 都用同一 key,删一条 → bytes 保留(另一条还在引用)。

    回归:refcount 没引入前,删消息会把同 key 的 bytes 一并误删,跨消息去重场景下
    另一条 message 的 media 变孤儿(reference 还在但找不到 bytes)。
    """
    await objectstore.put("media/shared.png", b"shared-bytes", None)
    # 同 file_id="fid-Y" + 同 key="media/shared.png" 的两条 message(模拟跨消息去重)
    base = _media_with_file_id("fid-Y")
    med = dataclasses.replace(
        base,
        object_key="media/shared.png",
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
        file_size=len(b"shared-bytes"),
    )
    await storage.save_message(
        make_message(
            channel_id=100,
            msg_id=10,
            text="first",
            media=[med],
        )
    )
    await storage.save_message(
        make_message(
            channel_id=100,
            msg_id=11,
            text="second",
            media=[med],
        )
    )
    mon = MonitorService(bus, client, storage, objectstore, settings)
    # 删其中一条
    await mon.delete_message(100, 10)
    # message 10 已删,message 11 还在
    assert await storage.get_message(100, 10) is None
    assert await storage.get_message(100, 11) is not None
    # key 仍被 message 11 引用 → bytes 保留
    assert await objectstore.exists("media/shared.png")
    assert await objectstore.get("media/shared.png") == b"shared-bytes"


def test_edited_message_ui_replace_message_renders_new_text():
    """UI 层:MessageView.replace_message 按 (channel_id, telegram_msg_id) 找 row,
    调 _format 重渲,row 数不变,文本更新到 v2。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])  # noqa: F841 — keep alive
    from tgmonitor.core.dto import MessageDTO
    from tgmonitor.ui.widgets.message_view import MessageListModel, MessageView

    view = MessageView()
    # 频道标题缓存,让 _format 输出稳定
    view.set_channel_titles({100: "TNews"})
    # 先 append 一条 v1
    msg1 = MessageDTO(
        id=0,
        channel_id=100,
        telegram_msg_id=1,
        date=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        text="v1",
        author="alice",
        media=[],
    )
    view.append(msg1)
    assert view.count() == 1
    # 编辑事件触发 replace_message(v2)
    msg2 = MessageDTO(
        id=0,
        channel_id=100,
        telegram_msg_id=1,
        date=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        text="v2-edited",
        author="alice",
        media=[],
    )
    view.replace_message(msg2)
    # row 数不变
    assert view.count() == 1
    # 内容更新 — 2026-09-02 v1.5.3 PR #D1:从 `view.item(0).data()` / `text()` 协议
    # 改为走 `model.data(idx, DtoRole)` / `FormattedRole`(QListWidget API 废除)。
    idx = view._model.index(0, 0)
    dto_after = view._model.data(idx, MessageListModel.DtoRole)
    assert isinstance(dto_after, MessageDTO)
    assert dto_after.text == "v2-edited"
    # 显示文本含 "v2-edited"
    formatted = view._model.data(idx, MessageListModel.FormattedRole)
    assert "v2-edited" in formatted


# ============================================================
# 2026-08-27 v1.4.0 PR #11:`MessageDeleted` 路由 → monitor 删 row + 清孤儿
# bytes。`delete_message` 已存在(用户主动删);新增 `_handle_message_deleted`
# 订阅总线事件,语义同但**不** republish(避免无限循环)。
# ============================================================


async def test_monitor_handles_message_deleted_removes_row(monitor, storage, client, bus) -> None:
    """PR #11:publish MessageDeleted → storage.delete_message 被调,row 真删。"""
    monitor.set_whitelist([100])
    await monitor.start()
    try:
        base = MessageDTO(
            id=0,
            channel_id=100,
            telegram_msg_id=99,
            date=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            text="x",
            author="alice",
            media=[],
        )
        await storage.save_message(base)
        # 确认初始存在
        assert await storage.get_message(100, 99) is not None
        await bus.publish(MessageDeleted(channel_id=100, telegram_msg_id=99))
        await asyncio.sleep(0)
        # row 已删
        assert await storage.get_message(100, 99) is None
    finally:
        await monitor.stop()


async def test_monitor_delete_handler_swallows_errors(monitor, storage, client, bus) -> None:
    """PR #11:storage 抛异常时 handler 不抛回 bus。"""
    await monitor.start()

    class BoomStorage:
        async def get_message(self, *a, **kw):
            raise RuntimeError("simulated")

    original = monitor.storage
    monitor.storage = BoomStorage()
    try:
        await bus.publish(MessageDeleted(channel_id=100, telegram_msg_id=1))
        await asyncio.sleep(0)
        # 异常被吞,不冒泡;后续其它订阅者继续工作
    finally:
        monitor.storage = original
        await monitor.stop()


async def test_monitor_delete_handler_does_not_republish(monitor, storage, client, bus) -> None:
    """PR #11:`_handle_message_deleted` 不 republish MessageDeleted(防无限循环)。

    监听总线计数 publish 次数 — 只能有我们主动 publish 的那 1 次。
    """
    await monitor.start()
    published: list = []

    async def _listener(event):
        published.append(event)

    bus.subscribe(MessageDeleted, _listener)
    try:
        # 注意:listener 在 monitor 之前 subscribe(EventBus 按订阅顺序执行);
        # monitor 自己用 _handle_message_deleted 也订阅,publish 次数不叠加 ——
        # 因为 monitor 的 handler 不调 publish。
        await bus.publish(MessageDeleted(channel_id=100, telegram_msg_id=1))
        await asyncio.sleep(0)
        # 仅 1 次(我们 publish 的那 1 次)
        assert len(published) == 1
    finally:
        bus.unsubscribe(MessageDeleted, _listener)
        await monitor.stop()


async def test_monitor_delete_handler_clears_orphan_bytes(
    monitor, storage, client, objectstore, bus
) -> None:
    """PR #11:删消息时,refcount=0 的 object_key 同步删 bytes。

    只有该 message 引用该 key → refcount 归 0 → objectstore 真删。
    """
    monitor.set_whitelist([100])
    await monitor.start()
    try:
        # 直接写一个 object key 到 objectstore
        med = MediaDTO(
            type=MediaType.PHOTO,
            mime_type="image/jpeg",
            file_name="orphan.jpg",
            file_size=10,
            object_key="media/orphan.jpg",
            object_backend="local",
            download_status=MediaDownloadStatus.DONE,
        )
        msg = MessageDTO(
            id=0,
            channel_id=100,
            telegram_msg_id=77,
            date=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            text="x",
            author="alice",
            media=[med],
        )
        await storage.save_message(msg)
        # objectstore 里存一份
        await objectstore.put("media/orphan.jpg", b"orphan-bytes")
        assert await objectstore.exists("media/orphan.jpg")
        # 删消息
        await bus.publish(MessageDeleted(channel_id=100, telegram_msg_id=77))
        await asyncio.sleep(0)
        # row 没了,bytes 也没了
        assert await storage.get_message(100, 77) is None
        assert not await objectstore.exists("media/orphan.jpg")
    finally:
        await monitor.stop()


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


async def test_monitor_delete_messages_no_cancel_event_runs_full(monitor, storage, bus) -> None:
    """回归修 #3a:不传 cancel_event 时行为不变(向后兼容)。"""
    for mid in range(1, 4):
        await storage.save_message(
            make_message(channel_id=100, msg_id=mid, text=f"msg-{mid}"),
        )

    items = [(100, m) for m in range(1, 4)]
    # 不传 cancel_event(默认 None)
    succeeded = await monitor.delete_messages(items)

    assert succeeded == 3
    for mid in range(1, 4):
        m = await storage.get_message(100, mid)
        assert m is None
