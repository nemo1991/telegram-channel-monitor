"""Media Manager 当前视图(per-media)CSV Exporter — 2026-08-25 v1.3.0 PR #7。

- 一行 = 一条 media(per-message 的 csv_exporter 是每条消息一行,不同)
- 13 列固定:`channel_id` / `channel_title` / `telegram_msg_id` /
  `message_date` / `media_idx` / `media_type` / `file_name` / `file_size` /
  `mime_type` / `download_status` / `download_error` / `object_key` /
  `object_backend`
- 列顺序固定,便于 pandas / Excel 模板复用
- 2026-08-27 v1.4.0 PR #17:`file_name` / `download_error` /
  `channel_title` 走 `_guard_csv_cell` 防 Excel 公式注入(CWE-1236)
  — `file_name` 是主攻击面(用户控制)

实现说明:为不破坏 `Exporter.render` ABC 协议,把每条目标 media 单独包成
一个 1-element `MediaDTO` 列表的临时 MessageDTO(覆盖原始 `media` 字段),
然后 exporter `render` 直接遍历 `messages`,对每条 message 取
`m.media[0]` 写一行。`media_idx` 列通过 dispatcher 在 service 层注入到
message 的临时属性 `_media_idx`(本 exporter 读它)。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tgmonitor.core.dto import ChannelDTO, ExportFormat, MessageDTO
from tgmonitor.core.export.base import Exporter, exporter
from tgmonitor.core.export.guards import _guard_csv_cell

if TYPE_CHECKING:
    from tgmonitor.core.objectstore.base import ObjectStore


MEDIA_CSV_COLUMNS: list[str] = [
    "channel_id",
    "channel_title",
    "telegram_msg_id",
    "message_date",
    "media_idx",
    "media_type",
    "file_name",
    "file_size",
    "mime_type",
    "download_status",
    "download_error",
    "object_key",
    "object_backend",
]


@exporter(ExportFormat.MEDIA_CSV)
class MediaListCsvExporter(Exporter):
    """Media Manager per-media CSV — 一行一条 media,所有列平铺。

    复用 `Exporter.render` 协议:dispatcher(`ExportService._run_media`)
    把每条目标 media 包成 `MessageDTO`(`media` 字段含 1 个元素,
    `_media_idx` 临时属性),exporter 直接 `m.media[0]` 写一行。`_media_idx`
    是 dispatcher ↔ exporter 的私有通道,导出协议外不可见。
    """

    format = ExportFormat.MEDIA_CSV

    async def render(
        self,
        out_path: Path,
        channels: dict[int, ChannelDTO],
        messages: list[MessageDTO],
        *,
        object_store: ObjectStore | None = None,
        include_thumbnails: bool = False,
    ) -> int:
        """写 per-media CSV → 返回字节数。"""
        with out_path.open("w", encoding="utf-8", newline="") as f:  # noqa: ASYNC240 — 渲染写盘同步
            w = csv.DictWriter(f, fieldnames=MEDIA_CSV_COLUMNS)
            w.writeheader()
            for m in messages:
                if not m.media:
                    continue
                med = m.media[0]
                ch = channels.get(m.channel_id)
                # 2026-08-25 v1.3.0 PR #7:`_media_idx` 由 service dispatcher 注入
                media_idx: Any = getattr(m, "_media_idx", -1)
                w.writerow(
                    {
                        "channel_id": m.channel_id,
                        "channel_title": _guard_csv_cell(ch.title if ch else ""),
                        "telegram_msg_id": m.telegram_msg_id,
                        "message_date": m.date.isoformat() if m.date else "",
                        "media_idx": media_idx,
                        "media_type": med.type.value,
                        # 2026-08-27 v1.4.0 PR #17:`file_name` / `download_error`
                        # 是用户可控 / 错误信息都可能以 = / + 开头 → 公式注入。
                        "file_name": _guard_csv_cell(med.file_name or ""),
                        "file_size": med.file_size if med.file_size is not None else "",
                        "mime_type": med.mime_type or "",
                        "download_status": med.download_status.value,
                        "download_error": _guard_csv_cell(med.download_error or ""),
                        "object_key": med.object_key or "",
                        "object_backend": med.object_backend or "",
                    }
                )
        return out_path.stat().st_size  # noqa: ASYNC240
