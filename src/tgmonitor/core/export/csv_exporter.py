"""CSV Exporter — 表格形式,便于 Excel / pandas。

- 一行 = 一条消息;媒体计数与首图类型入列
- 所有列展开平铺,无嵌套
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING

from tgmonitor.core.dto import ChannelDTO, ExportFormat, MessageDTO
from tgmonitor.core.export.base import Exporter, exporter

if TYPE_CHECKING:
    from tgmonitor.core.objectstore.base import ObjectStore

COLUMNS = [
    "channel_id",
    "channel_title",
    "telegram_msg_id",
    "date",
    "author",
    "text",
    "views",
    "forwards",
    "edited",
    "media_count",
    "media_types",
    "reply_to_msg_id",
]


@exporter(ExportFormat.CSV)
class CsvExporter(Exporter):
    format = ExportFormat.CSV

    async def render(
        self,
        out_path: Path,
        channels: dict[int, ChannelDTO],
        messages: list[MessageDTO],
        *,
        object_store: ObjectStore | None = None,
        include_thumbnails: bool = False,
    ) -> int:
        with out_path.open("w", encoding="utf-8", newline="") as f:  # noqa: ASYNC240 — 渲染线程受 GIL 阻塞,文件写入是 sync-only
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            for m in messages:
                ch = channels.get(m.channel_id)
                w.writerow(
                    {
                        "channel_id": m.channel_id,
                        "channel_title": ch.title if ch else "",
                        "telegram_msg_id": m.telegram_msg_id,
                        "date": m.date.isoformat() if m.date else "",
                        "author": m.author or "",
                        "text": m.text,
                        "views": m.views if m.views is not None else "",
                        "forwards": m.forwards if m.forwards is not None else "",
                        "edited": m.edited,
                        "media_count": len(m.media),
                        "media_types": "|".join(med.type.value for med in m.media),
                        "reply_to_msg_id": m.reply_to_msg_id if m.reply_to_msg_id is not None else "",
                    }
                )
        return out_path.stat().st_size  # noqa: ASYNC240 — 文件 IO 同步,与 write 同步完成
