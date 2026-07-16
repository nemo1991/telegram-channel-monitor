"""HTML Exporter — Jinja2 模板,可内嵌 base64 缩略图。

缩略图来源:`object_store.get(media.thumb_key)`,内嵌为 `data:image/jpeg;base64,...`。
"""
from __future__ import annotations

import base64
import mimetypes
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, select_autoescape

from tgmonitor.core.dto import ChannelDTO, ExportFormat, MessageDTO
from tgmonitor.core.export.base import Exporter, exporter

if TYPE_CHECKING:
    from tgmonitor.core.objectstore.base import ObjectStore


TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{{ title }}</title>
<style>
:root { color-scheme: light dark; }
body { font-family: -apple-system, "Segoe UI", sans-serif; max-width: 920px; margin: 2em auto; padding: 0 1em; }
header { border-bottom: 1px solid #ccc; padding-bottom: 1em; margin-bottom: 2em; }
h2 { margin-top: 2em; }
.msg { border: 1px solid #ddd; border-radius: 8px; padding: 1em; margin: 1em 0; background: #fafafa; }
@media (prefers-color-scheme: dark) { .msg { background: #1c1c1c; border-color: #333; } }
.meta { color: #888; font-size: 0.9em; }
.text { white-space: pre-wrap; margin: 0.5em 0; }
.media { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 0.5em; }
.media img { max-width: 200px; max-height: 200px; border-radius: 4px; }
.media .ph { font-size: 0.85em; color: #888; }
</style>
</head>
<body>
<header>
  <h1>📡 Telegram 频道导出</h1>
  <div class="meta">
    导出时间 {{ generated_at }} · {{ channel_count }} 频道 · {{ message_count }} 消息
  </div>
</header>
{% for cid, msgs in channels %}
<section>
  <h2>{{ channels_title[cid] }}</h2>
  {% for m in msgs %}
  <div class="msg">
    <div class="meta">
      {{ m.date.strftime('%Y-%m-%d %H:%M:%S') if m.date else '' }}
      · msg #{{ m.telegram_msg_id }}
      {% if m.author %} · {{ m.author }}{% endif %}
      {% if m.views is not none %} · 👁 {{ m.views }}{% endif %}
      {% if m.edited %} · <em>edited</em>{% endif %}
    </div>
    {% if m.text %}<div class="text">{{ m.text }}</div>{% endif %}
    {% if m.media %}
    <div class="media">
    {% for med in m.media %}
      {% if med.thumb_data_uri %}
        <img src="{{ med.thumb_data_uri }}" alt="{{ med.type.value }}" title="{{ med.file_name or '' }}">
      {% else %}
        <span class="ph">📎 {{ med.type.value }} {{ med.file_name or '' }}</span>
      {% endif %}
    {% endfor %}
    </div>
    {% endif %}
  </div>
  {% endfor %}
</section>
{% endfor %}
</body>
</html>
"""


@exporter(ExportFormat.HTML)
class HtmlExporter(Exporter):
    format = ExportFormat.HTML

    def __init__(self) -> None:
        self._env = Environment(autoescape=select_autoescape(["html"]))
        self._tmpl = self._env.from_string(TEMPLATE)

    async def render(
        self,
        out_path: Path,
        channels: dict[int, ChannelDTO],
        messages: list[MessageDTO],
        *,
        object_store: ObjectStore | None = None,
        include_thumbnails: bool = False,
    ) -> int:
        # 准备模板数据
        grouped: dict[int, list[MessageDTO]] = defaultdict(list)
        for m in messages:
            grouped[m.channel_id].append(m)

        if include_thumbnails and object_store is not None:
            for m in messages:
                for med in m.media:
                    if med.thumb_key:
                        try:
                            blob = await object_store.get(med.thumb_key)
                            mime = _guess_thumb_mime(med.thumb_key, med.mime_type)
                            med.thumb_data_uri = (  # type: ignore[attr-defined]
                                f"data:{mime};base64,{base64.b64encode(blob).decode()}"
                            )
                        except Exception:  # noqa: BLE001
                            pass

        channels_title = {cid: (ch.title if ch else f"#{cid}") for cid, ch in channels.items()}
        html = self._tmpl.render(
            title="Telegram 频道导出",
            generated_at=datetime.utcnow().isoformat(),
            channel_count=len(channels),
            message_count=len(messages),
            channels=grouped.items(),
            channels_title=channels_title,
        )
        out_path.write_text(html, encoding="utf-8")  # noqa: ASYNC240 — 文件 IO 同步,已在 IO 阻塞路径,不切线程
        return out_path.stat().st_size  # noqa: ASYNC240 — 同上


def _guess_thumb_mime(key: str, fallback: str | None) -> str:
    if fallback:
        return fallback
    guess, _ = mimetypes.guess_type(key)
    return guess or "image/jpeg"
