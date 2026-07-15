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
from tgmonitor.core.export import (
    csv_exporter,
    html_exporter,
    json_exporter,
    markdown_exporter,
)
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


async def test_registry_has_all_four():
    available = EXPORTERS.available()
    assert set(available) == {
        ExportFormat.JSON,
        ExportFormat.CSV,
        ExportFormat.MARKDOWN,
        ExportFormat.HTML,
    }
