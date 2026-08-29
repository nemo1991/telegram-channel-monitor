"""CSV Exporter — 表格形式,便于 Excel / pandas。

- 一行 = 一条消息;媒体计数与首图类型入列
- 所有列展开平铺,无嵌套
- 2026-08-27 v1.4.0 PR #17:`text` / `author` / `channel_title` 等用户/频道
  内容走 `_guard_csv_cell` 防 Excel 公式注入(CWE-1236)
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING

from tgmonitor.core.dto import ChannelDTO, ExportFormat, MessageDTO
from tgmonitor.core.export.base import Exporter, exporter
from tgmonitor.core.export.guards import _guard_csv_cell

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
    """CSV Exporter — 一行一条消息,所有列平铺,无嵌套。

    媒体列:`media_count` = 数量;`media_types` = `|` 分隔的类型名。
    `include_thumbnails` / `object_store` 参数 CSV 不用,保留 Protocol 形状。
    """

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
        """写 CSV → 返回字节数(便于进度回报)。"""
        with out_path.open("w", encoding="utf-8", newline="") as f:  # noqa: ASYNC240 — 渲染线程受 GIL 阻塞,文件写入是 sync-only
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            for m in messages:
                ch = channels.get(m.channel_id)
                w.writerow(
                    {
                        "channel_id": m.channel_id,
                        "channel_title": _guard_csv_cell(ch.title if ch else ""),
                        "telegram_msg_id": m.telegram_msg_id,
                        "date": m.date.isoformat() if m.date else "",
                        "author": _guard_csv_cell(m.author or ""),
                        "text": _guard_csv_cell(m.text),
                        "views": m.views if m.views is not None else "",
                        "forwards": m.forwards if m.forwards is not None else "",
                        "edited": m.edited,
                        "media_count": len(m.media),
                        "media_types": "|".join(med.type.value for med in m.media),
                        "reply_to_msg_id": m.reply_to_msg_id
                        if m.reply_to_msg_id is not None
                        else "",
                    }
                )
        return out_path.stat().st_size  # noqa: ASYNC240 — 文件 IO 同步,与 write 同步完成
