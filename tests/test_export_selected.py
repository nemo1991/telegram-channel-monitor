"""2026-09-08 v1.7.0:ExportService._run_selected 多选导出分支测试。

覆盖:
- selected_messages 非空 → 走 _run_selected(非 _run_messages)
- 跳过不存在的消息(get_message 返 None)
- 进度事件 ExportProgress(written, total) 按 N 累加
- 结果 ExportDone 含 message_count == 实际存在的数
- channels 子集只含选中消息所在频道
- 3 种格式(JSON / HTML / ZIP)都跑通
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tgmonitor.core.dto import (
    ChannelDTO,
    ExportFormat,
    ExportRequest,
    MessageDTO,
)
from tgmonitor.core.events import EventBus, ExportDone, ExportProgress
from tgmonitor.core.export.service import ExportService


class _StubStorage:
    """最小 StorageRepository stub — 只实现 get_message / list_channels。"""

    def __init__(self, msgs: dict[tuple[int, int], MessageDTO], channels: list[ChannelDTO]) -> None:
        self._msgs = msgs
        self._channels = channels

    async def get_message(self, channel_id: int, telegram_msg_id: int) -> MessageDTO | None:
        return self._msgs.get((channel_id, telegram_msg_id))

    async def list_channels(self) -> list[ChannelDTO]:
        return list(self._channels)


class _StubObjectStore:
    """最小 ObjectStore stub — 测试不需要真实存东西。"""

    async def get(self, key: str) -> bytes | None:
        return None

    async def put(self, key: str, data: bytes) -> str:
        return key

    async def delete(self, key: str) -> None:
        return None


@pytest.mark.asyncio
async def test_run_selected_dispatches_to_run_selected(tmp_path: Path) -> None:
    """ExportRequest.selected_messages 非空 → ExportService 走 _run_selected 分支。"""
    ch1 = ChannelDTO(id=1, title="ch1")
    msgs = {
        (1, 10): MessageDTO(id=1, channel_id=1, telegram_msg_id=10, text="hello"),
        (1, 11): MessageDTO(id=2, channel_id=1, telegram_msg_id=11, text="world"),
    }
    storage = _StubStorage(msgs, [ch1])
    objects = _StubObjectStore()
    bus = EventBus()
    done_events: list[ExportDone] = []
    bus.subscribe(ExportDone, lambda e: done_events.append(e))
    svc = ExportService(storage, objects, bus)  # type: ignore[arg-type]

    req = ExportRequest(
        channel_ids=[],
        format=ExportFormat.JSON,
        out_path=str(tmp_path / "selected.json"),
        selected_messages=[(1, 10), (1, 11)],
    )
    async for _ in svc.run(req):
        pass
    assert len(done_events) == 1
    assert done_events[0].result is not None
    assert done_events[0].result.message_count == 2


@pytest.mark.asyncio
async def test_run_selected_skips_missing_messages(tmp_path: Path) -> None:
    """选中的 (cid, mid) 在 storage 不存在 → 跳过(可能 UI 删除后立即调用)。"""
    ch1 = ChannelDTO(id=1, title="ch1")
    msgs = {
        (1, 10): MessageDTO(id=1, channel_id=1, telegram_msg_id=10, text="alive"),
        # (1, 999) 不存在
    }
    storage = _StubStorage(msgs, [ch1])
    bus = EventBus()
    done_events: list[ExportDone] = []
    bus.subscribe(ExportDone, lambda e: done_events.append(e))
    svc = ExportService(storage, _StubObjectStore(), bus)  # type: ignore[arg-type]

    req = ExportRequest(
        channel_ids=[],
        format=ExportFormat.JSON,
        out_path=str(tmp_path / "selected.json"),
        selected_messages=[(1, 10), (1, 999)],
    )
    async for _ in svc.run(req):
        pass
    assert done_events[0].result is not None
    assert done_events[0].result.message_count == 1  # 只导存在的


@pytest.mark.asyncio
async def test_run_selected_filters_channels_subset(tmp_path: Path) -> None:
    """ch2 没被选中 → 不出现在导出 HTML 里(只列选中消息所在频道)。"""
    ch1 = ChannelDTO(id=1, title="ch1")
    ch2 = ChannelDTO(id=2, title="ch2")
    msgs = {
        (1, 10): MessageDTO(id=1, channel_id=1, telegram_msg_id=10, text="a"),
    }
    storage = _StubStorage(msgs, [ch1, ch2])
    svc = ExportService(storage, _StubObjectStore(), EventBus())  # type: ignore[arg-type]

    req = ExportRequest(
        channel_ids=[],
        format=ExportFormat.HTML,
        out_path=str(tmp_path / "selected.html"),
        selected_messages=[(1, 10)],
    )
    async for _ in svc.run(req):
        pass
    # 验证文件存在 + 内容仅含 ch1,不含 ch2
    out = (tmp_path / "selected.html").read_text()
    assert "ch1" in out
    assert "ch2" not in out


@pytest.mark.asyncio
async def test_run_selected_emits_progress_per_5(tmp_path: Path) -> None:
    """7 条选 → 进度事件 written=5、written=7(每 5 条一次 + 末尾一次)。"""
    ch1 = ChannelDTO(id=1, title="ch1")
    msgs = {
        (1, i): MessageDTO(id=i, channel_id=1, telegram_msg_id=i, text=f"m{i}")
        for i in range(1, 8)  # 1..7
    }
    storage = _StubStorage(msgs, [ch1])
    bus = EventBus()
    progresses: list[ExportProgress] = []
    bus.subscribe(ExportProgress, lambda e: progresses.append(e))
    svc = ExportService(storage, _StubObjectStore(), bus)  # type: ignore[arg-type]

    req = ExportRequest(
        channel_ids=[],
        format=ExportFormat.JSON,
        out_path=str(tmp_path / "selected.json"),
        selected_messages=[(1, i) for i in range(1, 8)],
    )
    async for _ in svc.run(req):
        pass
    # 期望 written=5(i+1=5 时)+ written=7(末尾)= 至少 2 次,可能 3 次
    assert any(p.written == 5 and p.total == 7 for p in progresses)
    assert any(p.written == 7 and p.total == 7 for p in progresses)


@pytest.mark.asyncio
async def test_run_selected_zip_format(tmp_path: Path) -> None:
    """ZIP 格式走 _run_selected 也要正常 — 验证 object_store 透传。"""
    ch1 = ChannelDTO(id=1, title="ch1")
    msgs = {
        (1, 10): MessageDTO(
            id=1,
            channel_id=1,
            telegram_msg_id=10,
            text="z",
            date=datetime(2026, 9, 8, tzinfo=UTC),
        ),
    }
    storage = _StubStorage(msgs, [ch1])
    bus = EventBus()
    done_events: list[ExportDone] = []
    bus.subscribe(ExportDone, lambda e: done_events.append(e))
    svc = ExportService(storage, _StubObjectStore(), bus)  # type: ignore[arg-type]

    req = ExportRequest(
        channel_ids=[],
        format=ExportFormat.ZIP,
        out_path=str(tmp_path / "selected.zip"),
        selected_messages=[(1, 10)],
    )
    async for _ in svc.run(req):
        pass
    assert done_events[0].result is not None
    assert done_events[0].result.message_count == 1
    assert (tmp_path / "selected.zip").exists()


@pytest.mark.asyncio
async def test_run_selected_empty_list_no_crash(tmp_path: Path) -> None:
    """selected_messages 空列表 → 走 _run_selected 但 exporter 收 0 消息。

    行为:ExportDone 仍发(可能含 result=None + error 或 result.message_count=0)。
    这里只断言不抛。
    """
    ch1 = ChannelDTO(id=1, title="ch1")
    storage = _StubStorage({}, [ch1])
    svc = ExportService(storage, _StubObjectStore(), EventBus())  # type: ignore[arg-type]

    req = ExportRequest(
        channel_ids=[],
        format=ExportFormat.JSON,
        out_path=str(tmp_path / "selected.json"),
        selected_messages=[],
    )
    # 不应抛
    async for _ in svc.run(req):
        pass


@pytest.mark.asyncio
async def test_run_messages_does_not_use_selected_branch(tmp_path: Path) -> None:
    """selected_messages=None → 走 _run_messages 旧路径(不调 get_message)。"""
    # _StubStorage 没实现 list_messages — 走到 _run_selected 路径才会被调
    # get_message(只 1 round-trip);走 _run_messages 会调 list_messages,会 AttributeError。
    # 这里只确保 selected_messages=None 不调 get_message per-item
    ch1 = ChannelDTO(id=1, title="ch1")

    class _BoomStorage(_StubStorage):
        async def get_message(self, channel_id: int, telegram_msg_id: int):
            raise AssertionError("selected_messages=None should not call get_message")

        async def list_messages(self, *args, **kwargs):
            return []  # 旧路径需要

        async def list_channels_inner(self):  # 避免覆盖
            return [ch1]

    storage = _BoomStorage({}, [ch1])
    svc = ExportService(storage, _StubObjectStore(), EventBus())  # type: ignore[arg-type]

    req = ExportRequest(
        channel_ids=[],
        format=ExportFormat.JSON,
        out_path=str(tmp_path / "all.json"),
        # selected_messages=None(默认)→ _run_messages
    )
    async for _ in svc.run(req):
        pass
    # 不抛 = 走的是 _run_messages(没调 get_message)
