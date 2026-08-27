"""Markdown Exporter — 人类可读,按频道分组。

2026-08-27 v1.4.0 PR #17:用户消息 / 文件名 / 频道标题走 `_scrub_markdown`
防 Markdown 注入(CWE-79)。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tgmonitor.core.dto import ChannelDTO, ExportFormat, MessageDTO
from tgmonitor.core.export.base import Exporter, exporter
from tgmonitor.core.export.guards import _scrub_markdown

if TYPE_CHECKING:
    from tgmonitor.core.objectstore.base import ObjectStore


@exporter(ExportFormat.MARKDOWN)
class MarkdownExporter(Exporter):
    """Markdown Exporter — 人类可读,按频道分组(`## title` → `### msg`)。

    媒体列在每条消息下:`- 📎 **type** mime file_name (size)`,可读源 ID。
    """

    format = ExportFormat.MARKDOWN

    async def render(
        self,
        out_path: Path,
        channels: dict[int, ChannelDTO],
        messages: list[MessageDTO],
        *,
        object_store: ObjectStore | None = None,
        include_thumbnails: bool = False,
    ) -> int:
        """渲染 Markdown → 写文件 → 返回字节数。"""
        grouped: dict[int, list[MessageDTO]] = defaultdict(list)
        for m in messages:
            grouped[m.channel_id].append(m)

        lines: list[str] = []
        lines.append("# Telegram 频道导出")
        lines.append("")
        lines.append(f"- 导出时间: `{datetime.now(UTC).isoformat()}`")
        lines.append(f"- 频道数: {len(channels)},消息数: {len(messages)}")
        lines.append("")

        for cid, msgs in grouped.items():
            ch = channels.get(cid)
            # 2026-08-27 v1.4.0 PR #17:channel title 也走 scrub — 恶意频道
            # 名字可能含 `## ` 等,直接当 Markdown 渲染。
            title = _scrub_markdown(ch.title if ch else f"#{cid}")
            lines.append(f"## {title}")
            if ch and ch.username:
                lines.append(f"  (https://t.me/{ch.username})")
            lines.append("")
            for m in msgs:
                dt = m.date.isoformat() if m.date else ""
                head = f"### {dt}  ·  msg #{m.telegram_msg_id}"
                if m.author:
                    head += f"  ·  {_scrub_markdown(m.author)}"
                lines.append(head)
                lines.append("")
                if m.text:
                    lines.append(_scrub_markdown(m.text))
                    lines.append("")
                if m.media:
                    for med in m.media:
                        lines.append(
                            f"- 📎 **{med.type.value}**  "
                            f"{med.mime_type or ''}  "
                            f"{_scrub_markdown(med.file_name) if med.file_name else ''}  "
                            f"({_human_size(med.file_size)})"
                        )
                        if med.object_key:
                            lines.append(f"  - object_key: `{med.object_key}` (backend={med.object_backend})")
                    lines.append("")
        out_path.write_text("\n".join(lines), encoding="utf-8")  # noqa: ASYNC240 — 文件 IO 同步
        return out_path.stat().st_size  # noqa: ASYNC240 — 同上


def _human_size(n: int | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n //= 1024
    return str(n)
