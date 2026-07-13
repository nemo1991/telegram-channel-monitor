"""JSON Exporter — 完整 DTO 序列化,结构化、可程序消费。"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from tgmonitor.core.dto import ChannelDTO, ExportFormat, MessageDTO
from tgmonitor.core.export.base import Exporter, exporter

if TYPE_CHECKING:
    from tgmonitor.core.objectstore.base import ObjectStore


@exporter(ExportFormat.JSON)
class JsonExporter(Exporter):
    format = ExportFormat.JSON

    async def render(
        self,
        out_path: Path,
        channels: dict[int, ChannelDTO],
        messages: list[MessageDTO],
        *,
        object_store: ObjectStore | None = None,
        include_thumbnails: bool = False,
    ) -> int:
        payload = {
            "schema": "tgmonitor.export/v1",
            "channels": [asdict(c) for c in channels.values()],
            "messages": [_message_to_dict(m) for m in messages],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        out_path.write_text(text, encoding="utf-8")
        return out_path.stat().st_size


def _message_to_dict(m: MessageDTO) -> dict:
    d = asdict(m)
    # datetime / Enum → str
    if d.get("date"):
        d["date"] = m.date.isoformat()
    for media in d.get("media", []):
        if isinstance(media.get("type"), str) is False:
            media["type"] = str(media.get("type"))
    return d
