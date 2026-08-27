"""Exporter 快照测试 — JSON / CSV / Markdown / HTML。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tests.conftest import make_message, make_photo
from tgmonitor.core.dto import ChannelDTO, ExportFormat, ExportRequest
from tgmonitor.core.events import EventBus

# noqa: F401 — 这些 import 触发 `@exporter(...)` 类装饰器,把各 Exporter
# 注册到全局 EXPORTERS 注册表。即使模块里没有直接用类名,也得 import。
from tgmonitor.core.export.base import EXPORTERS
from tgmonitor.core.export.service import ExportService


async def _setup(tmp_path):
    bus = EventBus()
    from tests.conftest import InMemoryRepository
    from tgmonitor.core.objectstore.local_store import LocalObjectStore

    storage = InMemoryRepository()
    objects = LocalObjectStore(root=tmp_path / "media")
    await objects.connect()
    # 一张缩略图入 ObjectStore
    await objects.put("media/abc.jpg.thumb", b"\xff\xd8\xff\xd9fake-jpeg", None)

    ch1 = ChannelDTO(id=100, title="新闻频道", username="news")
    ch2 = ChannelDTO(id=200, title="Tech", username="tech")
    await storage.upsert_channel(ch1)
    await storage.upsert_channel(ch2)

    base = datetime(2026, 1, 1, 12, 0, 0)
    await storage.save_message(make_message(channel_id=100, msg_id=1, text="第一条", date=base))
    await storage.save_message(
        make_photo(channel_id=200, msg_id=1)
    )
    await storage.save_message(make_message(channel_id=200, msg_id=2, text="再见", date=base))

    return storage, objects, bus, [ch1, ch2]


def _req(fmt: ExportFormat, out_path: Path) -> ExportRequest:
    return ExportRequest(
        channel_ids=[100, 200],
        date_from=None,
        date_to=None,
        format=fmt,
        out_path=str(out_path),
    )


@pytest.mark.parametrize(
    "fmt,ext",
    [
        (ExportFormat.JSON, ".json"),
        (ExportFormat.CSV, ".csv"),
        (ExportFormat.MARKDOWN, ".md"),
        (ExportFormat.HTML, ".html"),
    ],
)
async def test_export_each_format(tmp_path, fmt, ext):
    storage, objects, bus, _ = await _setup(tmp_path)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / f"out{ext}"
    req = _req(fmt, out)
    async for _ in svc.run(req):
        pass
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    if fmt == ExportFormat.JSON:
        import json
        d = json.loads(text)
        assert d["schema"] == "tgmonitor.export/v1"
        assert len(d["messages"]) == 3
    elif fmt == ExportFormat.CSV:
        assert "新闻频道" in text
        assert "photo" in text  # media_types 列
    elif fmt == ExportFormat.MARKDOWN:
        assert "## 新闻频道" in text
        assert "photo" in text
    elif fmt == ExportFormat.HTML:
        assert "<html" in text
        assert "新闻频道" in text


async def test_export_htmlembeds_thumbnails(tmp_path):
    storage, objects, bus, _ = await _setup(tmp_path)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "out.html"
    req = ExportRequest(
        channel_ids=[200],
        format=ExportFormat.HTML,
        out_path=str(out),
        include_thumbnails=True,
    )
    async for _ in svc.run(req):
        pass
    html = out.read_text(encoding="utf-8")
    # base64 缩略图应被内嵌
    assert "data:image/jpeg;base64," in html


async def test_registry_has_all_five():
    """v1.3.0 PR #7:新增 MEDIA_CSV 后,registry 应有 5 个 format。"""
    available = EXPORTERS.available()
    assert set(available) == {
        ExportFormat.JSON,
        ExportFormat.CSV,
        ExportFormat.MARKDOWN,
        ExportFormat.HTML,
        ExportFormat.MEDIA_CSV,
    }


# ---- 2026-08-25 v1.3.0 PR #7:per-media CSV exporter + dispatcher ----------


async def test_registry_includes_media_csv():
    """PR #7:新 MEDIA_CSV 注册到 EXPORTERS。"""
    available = EXPORTERS.available()
    assert ExportFormat.MEDIA_CSV in available


async def test_media_csv_exporter_snapshot(tmp_path):
    """PR #7:MediaListCsvExporter 写 13 列 + 每条 media 一行,列顺序固定。"""
    import csv as csv_mod

    from tgmonitor.core.dto import MediaDownloadStatus, MediaType

    storage, objects, bus, _ = await _setup(tmp_path)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "media.csv"

    from tgmonitor.core.dto import MediaExportRequest

    req = MediaExportRequest(
        channel_id=None,
        status=None,
        media_type=None,
        search="",
        out_path=str(out),
    )
    async for _ in svc.run(req):
        pass

    assert out.exists()
    text = out.read_text(encoding="utf-8")
    reader = csv_mod.DictReader(text.splitlines())
    rows = list(reader)
    # fixture: ch100 msg1 + ch200 msg1(photo) + ch200 msg2 = 1 photo
    # 没有 message 带 media 的 fixture → make_photo + 2 plain → 1 row
    # 注:原始 _setup 只有 make_photo 1 条带 media
    assert len(rows) == 1
    row = rows[0]
    # 13 列固定顺序
    expected_cols = [
        "channel_id", "channel_title", "telegram_msg_id", "message_date",
        "media_idx", "media_type", "file_name", "file_size", "mime_type",
        "download_status", "download_error", "object_key", "object_backend",
    ]
    assert list(row.keys()) == expected_cols
    assert row["channel_id"] == "200"
    assert row["channel_title"] == "Tech"
    assert row["media_type"] == MediaType.PHOTO.value
    assert row["download_status"] == MediaDownloadStatus.PENDING.value


async def test_export_service_run_media_dispatch(tmp_path):
    """PR #7:ExportService.run(MediaExportRequest) → 走 _run_media 分支 →
    ExportDone 事件 payload.message_count 是 media 行数。
    """
    from tgmonitor.core.dto import MediaExportRequest
    from tgmonitor.core.events import ExportDone

    storage, objects, bus, _ = await _setup(tmp_path)
    received: list[ExportDone] = []

    async def _capture(e):
        received.append(e)

    bus.subscribe(ExportDone, _capture)

    svc = ExportService(storage, objects, bus)
    out = tmp_path / "media.csv"
    req = MediaExportRequest(out_path=str(out))
    async for _ in svc.run(req):
        pass

    assert len(received) == 1
    assert received[0].result is not None
    assert received[0].result.out_path == str(out)
    # fixture 1 photo + 2 plain = 1 media row
    assert received[0].result.message_count == 1


async def test_export_service_run_messages_unchanged(tmp_path):
    """PR #7:ExportRequest(老)走 _run_messages 分支 — 向后兼容。"""
    import csv as csv_mod

    storage, objects, bus, _ = await _setup(tmp_path)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "msg.csv"
    req = ExportRequest(
        channel_ids=[100, 200],
        format=ExportFormat.CSV,
        out_path=str(out),
    )
    async for _ in svc.run(req):
        pass
    assert out.exists()
    # header 仍是 per-message schema:含 media_count 列(per-media CSV 没有)
    reader = csv_mod.DictReader(out.read_text(encoding="utf-8").splitlines())
    assert "media_count" in (reader.fieldnames or [])
    assert "media_types" in (reader.fieldnames or [])
    rows = list(reader)
    # fixture 3 messages(1 photo + 2 plain)→ per-message 行数 = 3
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# 2026-08-27 v1.4.0 PR #12:导出分页 bug(>500 静默截断)— 加 4 个回归测试
# ---------------------------------------------------------------------------


async def _bulk_seed_messages(storage, channel_id: int, count: int) -> None:
    """批量塞 count 条 message 到 storage;时间从 2026-01-01 开始,id 1..count。

    用来触发 ExportService 的 PAGE_SIZE=500 分页边界。
    """
    from tests.conftest import make_message

    base = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(1, count + 1):
        await storage.save_message(
            make_message(
                channel_id=channel_id, msg_id=i, text=f"m{i}", date=base,
            )
        )


async def test_export_pagination_500_messages_complete(tmp_path):
    """PR #12:恰好 500 条 → 一次 PAGE_SIZE 拉完,不丢。"""
    import csv as csv_mod

    storage, objects, bus, _ = await _setup(tmp_path)
    await _bulk_seed_messages(storage, 100, 500)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "msg.csv"
    req = ExportRequest(channel_ids=[100], format=ExportFormat.CSV, out_path=str(out))
    async for _ in svc.run(req):
        pass
    rows = list(csv_mod.DictReader(out.read_text(encoding="utf-8").splitlines()))
    assert len(rows) == 500


async def test_export_pagination_501_messages_full(tmp_path):
    """PR #12:501 条 → 第二页拉 1 条,凑齐不丢(老实现只 500)。"""
    import csv as csv_mod

    storage, objects, bus, _ = await _setup(tmp_path)
    await _bulk_seed_messages(storage, 100, 501)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "msg.csv"
    req = ExportRequest(channel_ids=[100], format=ExportFormat.CSV, out_path=str(out))
    async for _ in svc.run(req):
        pass
    rows = list(csv_mod.DictReader(out.read_text(encoding="utf-8").splitlines()))
    # 老 bug 是「拉完一页 break」 → len == 500;新分页必须 == 501。
    assert len(rows) == 501


async def test_export_pagination_1001_messages_full(tmp_path):
    """PR #12:1001 条跨 3 页,sum == 1001。"""
    import csv as csv_mod

    storage, objects, bus, _ = await _setup(tmp_path)
    await _bulk_seed_messages(storage, 100, 1001)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "msg.csv"
    req = ExportRequest(channel_ids=[100], format=ExportFormat.CSV, out_path=str(out))
    async for _ in svc.run(req):
        pass
    rows = list(csv_mod.DictReader(out.read_text(encoding="utf-8").splitlines()))
    assert len(rows) == 1001


async def test_export_pagination_emits_progress_per_batch(tmp_path):
    """PR #12:1001 条 → ExportProgress 至少发 2 次(written=500, 1001)。"""
    storage, objects, bus, _ = await _setup(tmp_path)
    await _bulk_seed_messages(storage, 100, 1001)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "msg.csv"
    req = ExportRequest(channel_ids=[100], format=ExportFormat.CSV, out_path=str(out))

    progress_written: list[int] = []
    async def on_event(event) -> None:
        from tgmonitor.core.events import ExportProgress
        if isinstance(event, ExportProgress):
            progress_written.append(event.written)
    bus.subscribe_all(on_event)

    async for _ in svc.run(req):
        pass
    # 至少出现 500(第一页)和 1001(终态)两个 written
    assert 500 in progress_written
    assert progress_written[-1] == 1001
