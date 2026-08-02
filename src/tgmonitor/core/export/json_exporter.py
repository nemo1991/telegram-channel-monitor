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
    """JSON Exporter — 完整 DTO 序列化(`schema: tgmonitor.export/v1`)。

    结构化、可程序消费;datetime / Enum 自动转 str(`default=str` 兜底)。
    """

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
        """写 JSON → 返回字节数。`object_store` / `include_thumbnails` JSON 不用,仅保形。"""
        payload = {
            "schema": "tgmonitor.export/v1",
            "channels": [asdict(c) for c in channels.values()],
            "messages": [_message_to_dict(m) for m in messages],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        out_path.write_text(text, encoding="utf-8")  # noqa: ASYNC240 — 同 csv/html,写文件同步即可
        return out_path.stat().st_size  # noqa: ASYNC240 — 同上


def _message_to_dict(m: MessageDTO) -> dict:
    d = asdict(m)
    # datetime / Enum → str
    if d.get("date"):
        d["date"] = m.date.isoformat()
    for media in d.get("media", []):
        if isinstance(media.get("type"), str) is False:
            media["type"] = str(media.get("type"))
    return d
