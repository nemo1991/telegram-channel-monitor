"""2026-09-17 PR 4 mega-split:MonitorService media 下载 + 编辑路径测试。

11 tests:
- FULL 策略 + MediaDownloader(2):正常下载 / 下载失败
- 实时编辑路径(3):同 id 重推走 edit / 连续 3 次 / content 变更发 MessageEdited
- 字段覆盖编辑(1):text/views/forwards/edited/media 全部覆盖
- 编辑路径 + storage 空(1):罕见路径 — 当新增保存
- media 跨消息去重(2):storage find_media_by_file_id 命中 / _handle 阶段拷 storage 优先副本
- 下载失败(已在 FULL 策略里)— 独立覆盖

helper:`_media_with_file_id`(本文件独立重新定义 — 不跨文件 import)。
`_SlowDownloadClient`(让 `download_file` 睡 0.2s,确定性观察「落库 DOWNLOADING → 后台下载 → 回写 DONE」时序)。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from tests.conftest import make_message
from tests.fixtures._async import wait_for
from tgmonitor.core.config import MediaPolicy
from tgmonitor.core.dto import (
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
)
from tgmonitor.core.events import (
    MediaDownloaded,
    MessageEdited,
    MessageReceived,
)
from tgmonitor.core.monitor.service import MediaDownloader, MonitorService
from tgmonitor.core.telegram.fake_client import FakeTelegramClient


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


class _SlowDownloadClient(FakeTelegramClient):
    """`download_file` 睡 0.2s 再返回 — 模拟大文件下载耗时。

    让「先落库 DOWNLOADING → 后台下载 → 回写 DONE」的时序可确定性观察,
    而不是在 0.05s 的 sleep 里被秒完成下载的假 client 竞态掉。

    2026-09-01 v1.5.1 PR #B3:接 `progress_callback` kwarg — 与 Protocol
    签名对齐;SlowClient 不发进度(避免测试事件洪水)。
    """

    async def download_file(self, file_id: str, *, progress_callback=None) -> bytes | None:
        await asyncio.sleep(0.2)
        return await super().download_file(file_id, progress_callback=progress_callback)


async def test_full_policy_downloads_media_async(bus, storage, objectstore, settings) -> None:
    """FULL 策略 + MediaDownloader:新消息先落库(DOWNLOADING)→ 后台下载 →
    回写 DONE + object_key → 发 MediaDownloaded 事件;消息不阻塞落库。

    回归:大文件下载(最长 30 分钟)不再阻塞消息落库 —— 用户立即可见
    「下载中」状态,而不是看到有记录无文件的空窗。
    """
    client = _SlowDownloadClient()
    client.set_download("fid-1", b"image-bytes")
    full = settings.model_copy(update={"media_policy": MediaPolicy.FULL})
    mon = MonitorService(
        bus,
        client,
        storage,
        objectstore,
        full,
        downloader=MediaDownloader(client, storage, objectstore),
    )
    mon.set_whitelist([100])
    received: list[MessageReceived] = []
    downloaded: list[MediaDownloaded] = []
    downloaded_evt = asyncio.Event()

    async def _on_received(e: MessageReceived) -> None:
        received.append(e)

    async def _on_downloaded(e: MediaDownloaded) -> None:
        downloaded.append(e)
        downloaded_evt.set()

    bus.subscribe(MessageReceived, _on_received)
    bus.subscribe(MediaDownloaded, _on_downloaded)

    msg = make_message(
        channel_id=100,
        msg_id=10,
        text="media!",
        media=[_media_with_file_id("fid-1")],
    )
    await mon.start()
    try:
        await client.simulate_incoming(msg)
        # 等「消息已落库且 media 仍是 DOWNLOADING」 — 此状态窗持续 0.2s
        # (SlowClient 下载耗时),wait_for 立即命中,确定性高
        assert await wait_for(
            lambda: (
                len(received) == 1
                and (s := received[0].message.media[0]).download_status
                == MediaDownloadStatus.DOWNLOADING
            ),
            timeout=1.0,
        ), "消息没在 1s 内进入 DOWNLOADING 状态"
        stored = await storage.get_message(100, 10)
        assert stored is not None
        assert stored.media[0].download_status == MediaDownloadStatus.DOWNLOADING
        # 等 worker 完成下载并回写(超时报错)
        await asyncio.wait_for(downloaded_evt.wait(), timeout=2.0)
        assert len(downloaded) == 1
        med = downloaded[0].media
        assert med is not None
        assert med.download_status == MediaDownloadStatus.DONE
        assert med.object_key, "下载成功应回写 object_key"
        # storage 已回写 DONE
        stored = await storage.get_message(100, 10)
        assert stored is not None
        assert stored.media[0].download_status == MediaDownloadStatus.DONE
        assert stored.media[0].object_key == med.object_key
        # 对象存储真实有文件
        assert await objectstore.exists(med.object_key)
        assert await objectstore.get(med.object_key) == b"image-bytes"
    finally:
        await mon.stop()


async def test_full_policy_download_failure_marks_failed(
    bus, client, storage, objectstore, settings
) -> None:
    """下载失败 → 回写 FAILED + download_error(UI 可见原因),不阻塞消息落库。"""
    full = settings.model_copy(update={"media_policy": MediaPolicy.FULL})
    mon = MonitorService(
        bus,
        client,
        storage,
        objectstore,
        full,
        downloader=MediaDownloader(client, storage, objectstore),
    )
    mon.set_whitelist([100])
    downloaded: list[MediaDownloaded] = []
    downloaded_evt = asyncio.Event()

    async def _on_downloaded(e: MediaDownloaded) -> None:
        downloaded.append(e)
        downloaded_evt.set()

    bus.subscribe(MediaDownloaded, _on_downloaded)

    client.set_download("fid-2", None)  # 注入 None = 下载失败
    msg = make_message(
        channel_id=100,
        msg_id=11,
        text="broken",
        media=[_media_with_file_id("fid-2")],
    )
    await mon.start()
    try:
        await client.simulate_incoming(msg)
        await asyncio.wait_for(downloaded_evt.wait(), timeout=2.0)
        assert len(downloaded) == 1
        med = downloaded[0].media
        assert med is not None
        assert med.download_status == MediaDownloadStatus.FAILED
        assert med.download_error, "失败应带原因"
        assert med.object_key is None
        stored = await storage.get_message(100, 11)
        assert stored is not None
        assert stored.media[0].download_status == MediaDownloadStatus.FAILED
        assert stored.media[0].download_error
    finally:
        await mon.stop()


# ============================================================
# 2026-08-24:monitor dedup + edit-event path
# ============================================================


async def test_live_monitor_re_arrival_routes_to_edit_path(monitor, client, bus, storage) -> None:
    """同 id 重推(session 内)_seen_ids 命中 → 走 _handle_edited 而非 _handle。

    行为:
    - 第 1 次 push → MessageReceived(text=v1)+ save_message(v1)
    - 第 2 次 push(同 id,text=v2)→ _seen_ids 已记录 → 走 _handle_edited
      → MessageEdited + update_message(text=v2 覆盖)
    - MessageReceived 只发 1 次(编辑不发新消息事件,避免 UI 把它当新插入)
    """
    monitor.set_whitelist([100])
    received: list[MessageReceived] = []
    edited: list[MessageEdited] = []
    edited_evt = asyncio.Event()

    async def _on_recv(e: MessageReceived) -> None:
        received.append(e)

    async def _on_edit(e: MessageEdited) -> None:
        edited.append(e)
        edited_evt.set()

    bus.subscribe(MessageReceived, _on_recv)
    bus.subscribe(MessageEdited, _on_edit)

    await monitor.start()
    try:
        await client.simulate_incoming(make_message(channel_id=100, msg_id=1, text="v1"))
        assert await wait_for(lambda: len(received) >= 1, timeout=2.0), (
            "MessageReceived 没在 2s 内 emit"
        )
        # 第 2 次:同 id,v2 — 应走编辑路径
        await client.simulate_incoming(make_message(channel_id=100, msg_id=1, text="v2"))
        await asyncio.wait_for(edited_evt.wait(), timeout=2.0)
        # MessageReceived 仍 1 条(编辑不发新消息事件)
        assert len(received) == 1
        # MessageEdited 1 条,text 已是 v2
        assert len(edited) == 1
        assert edited[0].message is not None
        assert edited[0].message.text == "v2"
        # storage 文本被覆盖为 v2
        stored = await storage.get_message(100, 1)
        assert stored is not None and stored.text == "v2"
    finally:
        await monitor.stop()


async def test_live_monitor_silent_skip_when_message_in_storage(
    monitor, client, bus, storage
) -> None:
    """同 id 推 3 次 → 仅 1 条 MessageReceived,后 2 次都走编辑路径发 MessageEdited。

    与 test_live_monitor_re_arrival_routes_to_edit_path 互为补充:那个测 1 次
    重发,这个测连续 3 次,验证 _seen_ids 一致命中。
    """
    monitor.set_whitelist([100])
    received: list[MessageReceived] = []
    edited: list[MessageEdited] = []

    async def _on_recv(e: MessageReceived) -> None:
        received.append(e)

    async def _on_edit(e: MessageEdited) -> None:
        edited.append(e)

    bus.subscribe(MessageReceived, _on_recv)
    bus.subscribe(MessageEdited, _on_edit)

    await monitor.start()
    try:
        for i in range(3):
            await client.simulate_incoming(make_message(channel_id=100, msg_id=1, text=f"v{i}"))
        # 等 3 个 update 都处理完(received=1, edited=2)
        assert await wait_for(lambda: len(received) >= 1 and len(edited) >= 2, timeout=2.0), (
            f"3 个 update 没在 2s 内处理完:received={len(received)}, edited={len(edited)}"
        )
        # 仅 1 条 MessageReceived
        assert len(received) == 1
        # 2 条 MessageEdited
        assert len(edited) == 2
        # storage 文本被覆盖到最后一轮(v2)
        stored = await storage.get_message(100, 1)
        assert stored is not None and stored.text == "v2"
    finally:
        await monitor.stop()


async def test_full_policy_skips_download_when_storage_has_prior(
    monitor, client, bus, storage, objectstore, settings
) -> None:
    """跨消息 media 去重:msg 1 已下完,msg 2 同 file_id → 不重下,MediaDownloaded 不发。

    msg 2 落库时 media 已是 DONE + object_key 拷自 storage 优先副本。
    """
    client.set_download("fid-X", b"shared-bytes")
    full = settings.model_copy(update={"media_policy": MediaPolicy.FULL})
    mon = MonitorService(
        bus,
        client,
        storage,
        objectstore,
        full,
        downloader=MediaDownloader(client, storage, objectstore),
    )
    mon.set_whitelist([100])
    received: list[MessageReceived] = []
    downloaded: list[MediaDownloaded] = []
    downloaded_evt = asyncio.Event()

    async def _on_received(e: MessageReceived) -> None:
        received.append(e)

    async def _on_downloaded(e: MediaDownloaded) -> None:
        downloaded.append(e)
        downloaded_evt.set()

    bus.subscribe(MessageReceived, _on_received)
    bus.subscribe(MediaDownloaded, _on_downloaded)

    msg1 = make_message(
        channel_id=100,
        msg_id=10,
        text="first",
        media=[_media_with_file_id("fid-X")],
    )
    msg2 = make_message(
        channel_id=100,
        msg_id=11,
        text="second",
        media=[_media_with_file_id("fid-X")],
    )
    await mon.start()
    try:
        # msg 1:正常下载
        await client.simulate_incoming(msg1)
        await asyncio.wait_for(downloaded_evt.wait(), timeout=2.0)
        assert len(downloaded) == 1
        downloaded_evt.clear()
        # msg 2:同 file_id → _handle 阶段拷 storage 优先副本 → 不入下载队列
        await client.simulate_incoming(msg2)
        # 等 msg 2 落库(received=2);downloaded 应仍 1(dedup 命中)
        assert await wait_for(lambda: len(received) >= 2, timeout=2.0), (
            f"msg 2 没在 2s 内落库,received={len(received)}"
        )
        # 仅 msg 1 的下载事件
        assert len(downloaded) == 1, "msg 2 应命中 media dedup,不重下"
        # msg 2 落库时 media 已 DONE + object_key
        assert len(received) == 2
        msg2_received = received[1].message
        assert msg2_received is not None
        assert msg2_received.media[0].download_status == MediaDownloadStatus.DONE
        assert msg2_received.media[0].object_key, "应拷 storage 优先 object_key"
    finally:
        await mon.stop()


async def test_full_policy_dedup_cross_messages_via_storage(
    monitor, client, bus, storage, objectstore, settings
) -> None:
    """不同 file_name 但同 telegram_file_id → storage find_media_by_file_id 命中。

    与上面互补:这个测 storage skip #1(MediaDownloader.download_one 入口),
    上面测 _handle 阶段的 media dedup。两条路径一起覆盖。
    """
    client.set_download("fid-Y", b"only-once")
    full = settings.model_copy(update={"media_policy": MediaPolicy.FULL})
    mon = MonitorService(
        bus,
        client,
        storage,
        objectstore,
        full,
        downloader=MediaDownloader(client, storage, objectstore),
    )
    mon.set_whitelist([100])
    downloaded: list = []
    downloaded_evt = asyncio.Event()

    async def _on_dl(e):
        downloaded.append(e)
        downloaded_evt.set()

    bus.subscribe(MediaDownloaded, _on_dl)

    msg1 = make_message(
        channel_id=100,
        msg_id=20,
        text="a",
        media=[_media_with_file_id("fid-Y")],
    )
    await mon.start()
    try:
        await client.simulate_incoming(msg1)
        await asyncio.wait_for(downloaded_evt.wait(), timeout=2.0)
        assert len(downloaded) == 1
        downloaded_evt.clear()

        # 直接调 download_one:模拟 sync 重新拉这条 media(同 file_id)
        med2 = _media_with_file_id("fid-Y")
        assert mon.downloader is not None
        out = await mon.downloader.download_one(msg_pk=0, media=med2)
        # 应命中 storage skip #1 → DONE, 不调 client.download_file
        assert out.download_status == MediaDownloadStatus.DONE
        assert out.object_key, "拷自 storage"
        # 无新下载事件
        assert len(downloaded) == 1
    finally:
        await mon.stop()


async def test_live_monitor_emits_message_edited_on_content_change(
    monitor, client, bus, storage
) -> None:
    """先推 (100,1,v1),再推同 id v2(模拟 updateMessageContent)→ 发 MessageEdited。

    MessageReceived 只发 1 次(v1 时),编辑后 MessageEdited 触发,storage 文本被覆盖。
    """
    monitor.set_whitelist([100])
    received: list[MessageReceived] = []
    edited: list[MessageEdited] = []
    edited_evt = asyncio.Event()

    async def _on_edited(e: MessageEdited) -> None:
        edited.append(e)
        edited_evt.set()

    async def _on_recv(e: MessageReceived) -> None:
        received.append(e)

    bus.subscribe(MessageEdited, _on_edited)
    bus.subscribe(MessageReceived, _on_recv)

    await monitor.start()
    try:
        # 第 1 条:v1
        await client.simulate_incoming(make_message(channel_id=100, msg_id=1, text="v1"))
        assert await wait_for(lambda: len(received) >= 1, timeout=2.0), (
            "v1 MessageReceived 没在 2s 内 emit"
        )
        assert len(received) == 1
        # 第 2 条:同 id,text v2 — _seen_ids 命中 → 走 _handle_edited
        await client.simulate_incoming(make_message(channel_id=100, msg_id=1, text="v2"))
        await asyncio.wait_for(edited_evt.wait(), timeout=2.0)
        # 仍仅 1 条 MessageReceived(编辑不发)
        assert len(received) == 1
        # 1 条 MessageEdited,text 已覆盖
        assert len(edited) == 1
        assert edited[0].message is not None
        assert edited[0].message.text == "v2"
        # storage 也已覆盖
        stored = await storage.get_message(100, 1)
        assert stored is not None and stored.text == "v2"
    finally:
        await monitor.stop()


async def test_edit_path_overwrites_text_views_forwards_edited_media(
    monitor, client, bus, storage
) -> None:
    """编辑覆盖所有可变字段:text / views / forwards / edited / media。

    不动 message.id 与原 author 等不变字段(dataclasses.replace)。
    """
    monitor.set_whitelist([100])
    edited: list[MessageEdited] = []
    edited_evt = asyncio.Event()

    async def _on_edited(e: MessageEdited) -> None:
        edited.append(e)
        edited_evt.set()

    bus.subscribe(MessageEdited, _on_edited)

    # 第 1 条:initial
    initial = make_message(channel_id=100, msg_id=1, text="v1")
    # 第 2 条:edit,改 text + views + forwards + edited + media
    edited_dto = MessageDTO(
        id=0,
        channel_id=100,
        telegram_msg_id=1,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        text="v2-edited",
        author="alice",
        views=200,
        forwards=15,
        edited=True,
        media=[_media_with_file_id("new-fid")],
    )

    await monitor.start()
    try:
        await client.simulate_incoming(initial)
        # 等 initial 落库(确保 _seen_ids 已记录)
        assert await wait_for(lambda: storage.get_message(100, 1), timeout=2.0), (
            "initial message 没在 2s 内落库"
        )
        # 同 id,模拟 updateMessageContent — 改 fields
        await client.simulate_incoming(edited_dto)
        await asyncio.wait_for(edited_evt.wait(), timeout=2.0)

        stored = await storage.get_message(100, 1)
        assert stored is not None
        assert stored.text == "v2-edited"
        assert stored.views == 200
        assert stored.forwards == 15
        assert stored.edited is True
        assert len(stored.media) == 1
        assert stored.media[0].telegram_file_id == "new-fid"
        # author 未变(编辑字段表不含 author)
        assert stored.author == "alice"
    finally:
        await monitor.stop()


async def test_edit_path_when_storage_empty_saves_as_new(monitor, client, bus, storage) -> None:
    """编辑路径 + storage 空(罕见)→ 当作新增保存,仍发 MessageEdited。

    模拟:手动往 _seen_ids 注入 key(假装已见过),然后推 (100,1)
    → _seen_ids 命中 → 走 _handle_edited → storage.get_message 返 None
    → save_message + MessageEdited(不发 MessageReceived)。
    """
    monitor.set_whitelist([100])
    edited: list[MessageEdited] = []
    received: list[MessageReceived] = []

    async def _on_edited(e: MessageEdited) -> None:
        edited.append(e)

    async def _on_recv(e: MessageReceived) -> None:
        received.append(e)

    bus.subscribe(MessageEdited, _on_edited)
    bus.subscribe(MessageReceived, _on_recv)

    await monitor.start()
    try:
        # 手动在 _seen_ids 塞 (100, 1),但 storage 没这条消息
        monitor._seen_ids[(100, 1)] = None
        await client.simulate_incoming(make_message(channel_id=100, msg_id=1, text="edit-on-empty"))
        # 等 _handle_edited 落库并 emit MessageEdited
        assert await wait_for(lambda: len(edited) >= 1, timeout=2.0), (
            "MessageEdited 没在 2s 内 emit"
        )
        # MessageReceived 不发(编辑路径走 MessageEdited)
        assert received == []
        # MessageEdited 发,消息已落库
        assert len(edited) == 1
        assert edited[0].message.text == "edit-on-empty"
        stored = await storage.get_message(100, 1)
        assert stored is not None
        assert stored.text == "edit-on-empty"
    finally:
        await monitor.stop()
