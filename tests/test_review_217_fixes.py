"""2.1.7 审核止血回归( docs/CODE-REVIEW-2.1.7.md 的 PR-A)。

逐条对应报告里的条目:
- P1-1  `open_media` 扩展名白名单(本地落盘文件 + S3 暂存两条路)
- P1-2  bus 订阅不随 pause/resume 累积,且回调仍晚绑定
- P1-3  队列满时 `close()` 不挂死,阻塞中的生产端能退出
- P1-6  消息页日期预设 / 导出对话框日期给 aware 本地时间
- P1-8  `config` 的 URL 型字段抹掉凭据;代理解析错误不带出密码
- P2-1  并发批量时,先结束的 op 不清掉后注册 op 的取消信号
- P2-2  `.part` 唯一临时名 + `iter_keys` 跳过写入中的临时文件
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MediaType, MessageDTO
from tgmonitor.core.events import (
    BatchDone,
    ChannelMetadataChanged,
    ChannelPhotoChanged,
    ChannelTitleChanged,
    ConnectionStateChanged,
    MessageDeleted,
    MessageInteractionsChanged,
    MessagePinChanged,
)
from tgmonitor.core.objectstore import folder_store, local_store
from tgmonitor.core.objectstore._fs_base import LocalFsStoreBase
from tgmonitor.core.telegram.proxy import parse_socks5_proxy
from tgmonitor.core.telegram.telethon_client import _TelethonUpdateStream

# ---- P1-1 扩展名白名单 ------------------------------------------------------


def _done_media(file_name: str, mime: str, key: str) -> MediaDTO:
    """DONE 状态、带 object_key 的 media(模拟已下载完成)。"""
    return MediaDTO(
        type=MediaType.DOCUMENT,
        mime_type=mime,
        file_name=file_name,
        file_size=3,
        telegram_file_id="fid",
        object_key=key,
        object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )


def _msg(channel_id: int, msg_id: int, media: list[MediaDTO]) -> MessageDTO:
    return MessageDTO(
        id=0,
        channel_id=channel_id,
        telegram_msg_id=msg_id,
        text="hi",
        media=media,
    )


@pytest.mark.parametrize(
    ("file_name", "mime", "expected"),
    [
        ("photo.JPG", "image/jpeg", "jpg"),
        ("archive.tar.gz", "", "gz"),
        ("README", "text/plain", "txt"),  # 无点文件名 → 按 mime 推断
        ("x", "", "bin"),
        ("clip", "video/mp4", "mp4"),
    ],
)
def test_staged_suffix_priority(file_name: str, mime: str, expected: str) -> None:
    from tgmonitor.core.media_service._media_os_actions import _staged_suffix

    assert _staged_suffix(_done_media(file_name, mime, "media/x")) == expected


@pytest.mark.parametrize(
    "suffix",
    ["bat", "cmd", "com", "exe", "hta", "jse", "lnk", "ps1", "vbs", "html", "svg", "", None],
)
def test_executable_and_script_suffixes_are_not_safe_to_open(suffix: str | None) -> None:
    from tgmonitor.core.media_service._media_os_actions import _is_safe_to_open

    assert _is_safe_to_open(suffix) is False


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("C:/media/abc.jpg", "jpg"),
        ("media/abc.MP4", "mp4"),
        ("media/abc.tar.gz", "gz"),
        ("media/abc", None),
        ("media/.bashrc", None),  # 点开头的单段名:os.path.splitext 视为无扩展名
    ],
)
def test_suffix_of_takes_the_last_extension_lowercase(name: str, expected: str | None) -> None:
    from tgmonitor.core.media_service._media_os_actions import _suffix_of

    assert _suffix_of(name) == expected


@pytest.mark.parametrize("name", ["a.jpg.bat", "a.BAT ", "a.exe.", "a.html"])
def test_disguised_executables_do_not_pass_the_allowlist(name: str) -> None:
    from tgmonitor.core.media_service._media_os_actions import _is_safe_to_open, _suffix_of

    assert _is_safe_to_open(_suffix_of(f"media/{name}")) is False


def _capture_open_url(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把 core 的 open_url callback 换成只记录路径、强返 True 的桩。"""
    calls: list[str] = []
    monkeypatch.setattr(
        "tgmonitor.core.media_service._qt_bridge._module_open_url_cb",
        lambda path: calls.append(path) or True,
    )
    return calls


async def test_open_media_refuses_executable_stored_locally(
    app, storage, objectstore, monkeypatch
) -> None:
    """默认本地后端:落盘文件名带远端扩展名(`media/<hash>.bat`),不交给系统默认程序。"""
    calls = _capture_open_url(monkeypatch)
    await storage.save_message(_msg(100, 1, [_done_media("payload.bat", "", "media/abc123.bat")]))
    await objectstore.put("media/abc123.bat", b"MZ", None)

    result = await app.media.open_media_with_result(100, 1, 0)

    assert result.success is False
    assert result.error is not None and ".bat" in result.error
    assert calls == []


async def test_open_media_still_opens_dotless_document_stored_locally(
    app, storage, objectstore, monkeypatch
) -> None:
    """无点文件名(`README`)的 key 扩展名就是整个文件名,系统不认识 —— 按 mime 安全即放行。"""
    calls = _capture_open_url(monkeypatch)
    await storage.save_message(
        _msg(100, 1, [_done_media("README", "text/plain", "media/ab.README")])
    )
    await objectstore.put("media/ab.README", b"hello", None)

    result = await app.media.open_media_with_result(100, 1, 0)

    assert result.success is True, result
    assert len(calls) == 1


async def test_open_media_still_opens_regular_media_stored_locally(
    app, storage, objectstore, monkeypatch
) -> None:
    calls = _capture_open_url(monkeypatch)
    await storage.save_message(_msg(100, 1, [_done_media("cat.jpg", "image/jpeg", "media/ab.jpg")]))
    await objectstore.put("media/ab.jpg", b"jpeg", None)

    result = await app.media.open_media_with_result(100, 1, 0)

    assert result.success is True, result
    assert len(calls) == 1


async def test_open_media_refuses_script_from_remote_backend(app, storage, monkeypatch) -> None:
    """远端(S3 类)后端:暂存文件的扩展名取自远端 `file_name`,落盘前先拒绝。"""
    calls = _capture_open_url(monkeypatch)
    fetched: list[str] = []

    class _RemoteStore:
        def local_path(self, key: str) -> None:
            return None

        def uri(self, key: str) -> str:
            return f"s3://bucket/{key}"

        async def get(self, key: str) -> bytes:
            fetched.append(key)
            return b"payload"

    app.objects = _RemoteStore()  # type: ignore[assignment]
    await storage.save_message(_msg(100, 1, [_done_media("invoice.hta", "", "media/ab.bin")]))

    result = await app.media.open_media_with_result(100, 1, 0)

    assert result.success is False
    assert result.error is not None and ".hta" in result.error
    assert fetched == []  # 根本没去拉对象
    assert calls == []


# ---- P1-2 bus 订阅不累积 ----------------------------------------------------


_SUBSCRIBED_EVENTS = (
    MessageInteractionsChanged,
    MessagePinChanged,
    MessageDeleted,
    ChannelMetadataChanged,
    ChannelTitleChanged,
    ChannelPhotoChanged,
    ConnectionStateChanged,
)


async def test_bus_subscriptions_do_not_grow_across_pause_resume(monitor, bus) -> None:
    """pause/resume = stop → start;三轮后每类事件仍只有 1 个订阅者。"""
    monitor._HEARTBEAT_INTERVAL = 0.1  # type: ignore[attr-defined]
    for _ in range(3):
        await monitor.start()
        await monitor.stop()
    for event_type in _SUBSCRIBED_EVENTS:
        assert len(bus._subs.get(event_type, [])) == 1, event_type  # type: ignore[attr-defined]


async def test_bus_dispatch_reaches_callback_swapped_after_restart(
    monitor, bus, monkeypatch
) -> None:
    """订阅只登记一次,所以必须调用时才解析回调槽 —— 换掉宿主方法后新实现生效且只跑一次。"""
    monitor._HEARTBEAT_INTERVAL = 0.1  # type: ignore[attr-defined]
    await monitor.start()
    await monitor.stop()
    seen: list[MessagePinChanged] = []

    async def _stub(event: MessagePinChanged) -> None:
        seen.append(event)

    monkeypatch.setattr(monitor, "_handle_pin_changed", _stub)
    await bus.publish(MessagePinChanged(channel_id=100, telegram_msg_id=5, is_pinned=True))
    await bus.publish(MessagePinChanged(channel_id=100, telegram_msg_id=6, is_pinned=False))

    assert [e.telegram_msg_id for e in seen] == [5, 6]
    await monitor.stop()


# ---- P1-3 队列满时关流 ------------------------------------------------------


def _dto() -> MessageDTO:
    return MessageDTO(
        id=0,
        channel_id=-1001,
        telegram_msg_id=1,
        text="m",
        date=datetime.now(UTC),
    )


async def _fill(stream: _TelethonUpdateStream) -> None:
    for _ in range(stream._QUEUE_MAXSIZE):  # type: ignore[attr-defined]
        await stream.push(_dto())


async def test_update_stream_close_returns_promptly_when_queue_full() -> None:
    """旧实现 `await put(None)` 在满队列上永远等不到位置 → 暂停 / 退出挂死。"""
    closed: list[_TelethonUpdateStream] = []
    stream = _TelethonUpdateStream(on_close=closed.append)
    await _fill(stream)

    await asyncio.wait_for(stream.close(), timeout=1.0)

    assert closed == [stream]
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
    await asyncio.wait_for(stream.close(), timeout=1.0)  # 幂等
    assert closed == [stream]


async def test_update_stream_blocked_producer_exits_after_close() -> None:
    """反压下卡在 `push` 上的生产端,关流后必须自行退出(否则 Telethon 分发链被堵死)。"""
    stream = _TelethonUpdateStream()
    stream._PUT_WAIT_INTERVAL = 0.01  # type: ignore[attr-defined]
    await _fill(stream)
    producer = asyncio.create_task(stream.push(_dto()))
    await asyncio.sleep(0.05)
    assert not producer.done()  # 满时确实在阻塞

    await stream.close()
    await asyncio.wait_for(producer, timeout=1.0)

    assert stream._queue.qsize() == stream._QUEUE_MAXSIZE  # type: ignore[attr-defined]


async def test_update_stream_push_after_close_is_dropped() -> None:
    stream = _TelethonUpdateStream()
    await stream.close()
    await stream.push(_dto())
    # 队列里只有关流时塞进去的哨兵,关流后 push 的消息被丢弃
    assert stream._queue.qsize() == 1  # type: ignore[attr-defined]
    assert stream._queue.get_nowait() is None  # type: ignore[attr-defined]


# ---- P1-6 日期预设时区 ------------------------------------------------------


def test_preset_date_range_is_aware_and_covers_local_days() -> None:
    from tgmonitor.ui.widgets.message_manager_widget import _preset_date_range

    now = datetime(2026, 11, 26, 9, 0)  # naive = 本地时间
    start, end = _preset_date_range("today", now)
    assert start.tzinfo is not None and end.tzinfo is not None
    assert end - start == timedelta(days=1) - timedelta(microseconds=1)

    start7, _ = _preset_date_range("7d", now)
    assert (start7.year, start7.month, start7.day) == (2026, 11, 20)
    start30, end30 = _preset_date_range("30d", now)
    assert (start30.year, start30.month, start30.day) == (2026, 10, 28)
    assert (end30.year, end30.month, end30.day) == (2026, 11, 26)


@pytest.mark.skipif(sys.platform == "win32", reason="time.tzset 仅 POSIX")
async def test_today_preset_matches_early_morning_message_in_utc8(tmp_path) -> None:
    """东八区 07:30 的消息必须落在「今天」里(旧实现给 naive 本地时间,被当成 UTC 比较)。"""
    from tgmonitor.core.storage.sqlite_repo import SqliteRepository
    from tgmonitor.ui.widgets.message_manager_widget import _preset_date_range

    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        repo = SqliteRepository(tmp_path / "t.db")
        await repo.connect()
        await repo.init_schema()
        # 2026-11-26 07:30 (UTC+8) == 2026-11-25T23:30Z
        await repo.save_message(
            MessageDTO(
                id=0,
                channel_id=1,
                telegram_msg_id=1,
                author="a",
                text="local-morning",
                date=datetime(2026, 11, 25, 23, 30, tzinfo=UTC),
            )
        )
        date_from, date_to = _preset_date_range("today", datetime(2026, 11, 26, 9, 0))
        rows = await repo.list_messages([1], date_from=date_from, date_to=date_to)
        await repo.close()
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()

    assert len(rows) == 1
    assert rows[0].text == "local-morning"


def test_export_dialog_date_is_aware() -> None:
    from tgmonitor.ui.widgets.export_dialog import ExportDialog

    parsed = ExportDialog._parse_date("2026-11-20")
    assert parsed is not None and parsed.tzinfo is not None
    assert parsed.hour == 0 and parsed.minute == 0
    assert ExportDialog._parse_date("") is None
    assert ExportDialog._parse_date("not-a-date") is None


# ---- P1-8 凭据不外泄 --------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        (
            "db_dsn",
            "postgresql://user:secret@db.local:5432/tgmonitor",
            "postgresql://****@db.local:5432/tgmonitor",
        ),
        ("proxy", "socks5://user:secret@127.0.0.1:1080", "socks5://****@127.0.0.1:1080"),
        ("proxy", "socks5://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
        ("proxy", None, None),
        ("api_hash", "abcdef", "****"),
        ("db_backend", "sqlite", "sqlite"),
    ],
)
def test_config_mask_hides_url_credentials(key: str, value: object, expected: object) -> None:
    from tgmonitor.cli.config import _mask

    assert _mask(key, value) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://user:secret@127.0.0.1:1080",  # 不支持的 scheme
        "socks5://user:secret@127.0.0.1",  # 缺端口
        "socks5://user:secret@127.0.0.1:abc",  # 端口非数字
    ],
)
def test_proxy_parse_error_does_not_leak_credentials(url: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_socks5_proxy(url)
    assert "secret" not in str(excinfo.value)


# ---- P2-1 并发批量取消 ------------------------------------------------------


async def test_cancel_reaches_later_op_after_earlier_op_finished(batch_app, collected) -> None:
    """先注册的 op 先结束,不能把后注册、仍在跑的 op 的取消信号清掉。"""
    gate = asyncio.Event()

    async def _fast_pin(_cid, _mids, **_kw):
        return None

    async def _slow_react(_cid, _mid, _emoji, **_kw):
        await gate.wait()

    batch_app.client.pin_messages.side_effect = _fast_pin  # type: ignore[attr-defined]
    batch_app.client.add_reaction.side_effect = _slow_react  # type: ignore[attr-defined]
    await batch_app.batch.pin_messages([(1, 1)])  # op1 已跑完并退出
    op2 = asyncio.create_task(batch_app.batch.add_reaction([(7, 1), (7, 2)], "🔥"))
    await asyncio.sleep(0.02)  # op2 阻塞在第一条 RPC

    batch_app.batch.cancel_current_batch()
    gate.set()
    await asyncio.wait_for(op2, timeout=2.0)

    done = [e for e in collected if isinstance(e, BatchDone) and e.op == "react"]
    assert len(done) == 1
    assert done[0].error == "cancelled"
    assert done[0].succeeded == 1  # 已发出的那条 best-effort 算成功


# ---- P2-2 唯一临时名 --------------------------------------------------------


_STORE_CLASSES = [local_store.LocalObjectStore, folder_store.FolderObjectStore]


@pytest.mark.parametrize("store_cls", _STORE_CLASSES)
async def test_iter_keys_skips_in_flight_tmp_files(tmp_path, store_cls) -> None:
    """写入中的临时文件不是对象 —— Prune Orphans 不能把半成品当孤儿删。"""
    store = store_cls(root=tmp_path)
    await store.connect()
    await store.put("media/a.jpg", b"a", None)
    target = store.local_path("media/a.jpg")
    assert target is not None
    # 在对象旁边放一个「写入中」的临时文件,以及一个名字以 .part 结尾的真实对象
    (target.parent / ".tgmonitor-0123456789ab.jpg.part").write_bytes(b"half")
    await store.put("media/b.part", b"real object", None)

    keys = sorted([key async for key in store.iter_keys()])

    # `media/b.part` 是合法 key(远端文件可以叫 video.mp4.part),绝不能被当成临时文件漏掉
    assert keys == ["media/a.jpg", "media/b.part"]


@pytest.mark.parametrize("store_cls", _STORE_CLASSES)
async def test_concurrent_put_stream_same_key_does_not_interleave(tmp_path, store_cls) -> None:
    """同 key 并发写:两个写者各写各的临时文件,最终内容是其中一方完整的数据。"""
    store = store_cls(root=tmp_path)
    await store.connect()

    async def _payload(byte: bytes):
        async def _gen():
            for _ in range(50):
                yield byte * 100
                await asyncio.sleep(0)  # 让出,强制两个写者交错推进

        return _gen()

    first, second = await asyncio.gather(
        store.put_stream("media/x.bin", await _payload(b"A")),
        store.put_stream("media/x.bin", await _payload(b"B")),
    )
    assert first.size == second.size == 5000

    assert await store.get("media/x.bin") in (b"A" * 5000, b"B" * 5000)
    assert list(tmp_path.rglob("*.part")) == []  # 没有残留临时文件


def test_tmp_names_are_unique_per_writer(tmp_path) -> None:
    target = tmp_path / "media" / "abc.jpg"
    names = {LocalFsStoreBase._tmp_path_for(target).name for _ in range(200)}
    assert len(names) == 200
    name = names.pop()
    assert name.startswith(".tgmonitor-") and name.endswith(".jpg.part")


# ---- P2-2 Windows 竞态(并发首次写入同一目录时踩到)---------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("\\\\?\\C:\\Users\\me\\media\\x.bin", "C:\\Users\\me\\media\\x.bin"),
        ("\\\\?\\UNC\\server\\share\\media\\x.bin", "\\\\server\\share\\media\\x.bin"),
        ("C:\\Users\\me\\media\\x.bin", "C:\\Users\\me\\media\\x.bin"),
    ],
)
def test_without_extended_prefix_is_a_pure_text_normalization(given: str, expected: str) -> None:
    from tgmonitor.core.objectstore._fs_base import _without_extended_prefix

    assert str(_without_extended_prefix(Path(given))) == expected


@pytest.mark.skipif(os.name != "nt", reason="长路径前缀仅 Windows 的 resolve() 会产生")
async def test_ensure_inside_root_accepts_extended_length_prefix(tmp_path) -> None:
    """Windows 上 `resolve()` 在父目录正被创建的瞬间返回 `\\?\\C:\\...`,不能误判越界。"""
    store = local_store.LocalObjectStore(root=tmp_path)
    (tmp_path / "media").mkdir()
    extended = Path("\\\\?\\" + str(tmp_path / "media" / "x.bin"))

    store._ensure_inside_root(extended)  # 不抛

    with pytest.raises(ValueError):
        store._ensure_inside_root(Path("\\\\?\\" + str(tmp_path.parent / "outside.bin")))


def test_replace_retries_transient_permission_error(monkeypatch, tmp_path) -> None:
    """目标刚被另一个写者替换掉 → Windows 短暂 ACCESS_DENIED → 重试后成功。"""
    import tgmonitor.core.objectstore._fs_base as fs_base

    real_replace = os.replace
    attempts: list[int] = []
    src, dst = tmp_path / "a.part", tmp_path / "a.bin"
    src.write_bytes(b"x")

    def _flaky_replace(s, d):
        attempts.append(1)
        if len(attempts) <= 2:
            raise PermissionError(13, "Access is denied (delete pending)")
        real_replace(s, d)

    monkeypatch.setattr(fs_base.os, "replace", _flaky_replace)
    monkeypatch.setattr(fs_base, "_REPLACE_DELAY_S", 0.001)
    fs_base._replace_atomic(src, dst)

    assert len(attempts) == 3
    assert dst.read_bytes() == b"x" and not src.exists()


def test_replace_gives_up_on_persistent_permission_error(monkeypatch, tmp_path) -> None:
    """真权限问题不该无限重试 —— 用尽次数后照常上抛。"""
    import tgmonitor.core.objectstore._fs_base as fs_base

    def _always_denied(_s, _d):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(fs_base.os, "replace", _always_denied)
    monkeypatch.setattr(fs_base, "_REPLACE_ATTEMPTS", 3)
    monkeypatch.setattr(fs_base, "_REPLACE_DELAY_S", 0.001)
    src = tmp_path / "a.part"
    src.write_bytes(b"x")

    with pytest.raises(PermissionError):
        fs_base._replace_atomic(src, tmp_path / "a.bin")


def test_mkdir_parents_retries_transient_permission_error(monkeypatch, tmp_path) -> None:
    """两个线程同时创建同一新目录 → Windows 对后来者返回 ACCESS_DENIED。"""
    import tgmonitor.core.objectstore._fs_base as fs_base

    calls: list[int] = []
    target = tmp_path / "new" / "dir"
    real_mkdir = Path.mkdir

    def _flaky_mkdir(self, *args, **kwargs):
        calls.append(1)
        if self == target and len(calls) == 1:
            raise PermissionError(13, "Access is denied")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _flaky_mkdir)
    monkeypatch.setattr(fs_base, "_REPLACE_DELAY_S", 0.001)
    fs_base._mkdir_parents(target)

    assert len(calls) >= 2
    assert target.is_dir()
